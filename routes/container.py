"""
Container Management Routes
Endpoints for starting, stopping, and managing challenge containers
"""

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db
from models.container import ContainerInstance
from models.challenge import Challenge
from services.challenge_catalog import is_challenge_visible, visible_challenges_query
from services.container_manager import container_orchestrator
from datetime import datetime

container_bp = Blueprint('container', __name__, url_prefix='/container')


def container_access_error(challenge_id):
    challenge = db.session.get(Challenge, challenge_id)
    if is_challenge_visible(challenge, current_user):
        # The orchestrator's player operations are keyed by owner and challenge.
        # A running instance may still belong to the owner's previous team.
        stale_instance = ContainerInstance.query.filter(
            ContainerInstance.challenge_id == challenge_id,
            ContainerInstance.user_id == current_user.id,
            ContainerInstance.status.in_(['starting', 'running']),
            ContainerInstance.team_id.is_distinct_from(current_user.team_id),
        ).first()
        if not stale_instance:
            return None
    return jsonify(success=False, error='Challenge not found'), 404


def hide_container_limit_details(result):
    if 'existing_challenge_id' not in result:
        return result
    existing_id = result['existing_challenge_id']
    if existing_id and not container_access_error(existing_id):
        return result
    return {
        'success': False,
        'error': 'You already have a container running. Please stop it before starting a new one.',
    }

@container_bp.route('/start', methods=['POST'])
@login_required
def start_container():
    """Start a Docker container for a challenge"""
    try:
        data = request.get_json()
        challenge_id = data.get('challenge_id')
        
        if not challenge_id:
            return jsonify({'success': False, 'error': 'Missing challenge_id'}), 400
        error = container_access_error(challenge_id)
        if error:
            return error
        
        # Get user's team (if any)
        team = current_user.get_team()
        team_id = team.id if team else None
        
        # Start container
        result = hide_container_limit_details(container_orchestrator.start_container(
            challenge_id=challenge_id,
            user_id=current_user.id,
            ip_address=request.remote_addr or '0.0.0.0',
            team_id=team_id
        ))
        
        if result['success']:
            return jsonify(result), 200
        else:
            return jsonify(result), 400
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}'
        }), 500


@container_bp.route('/stop', methods=['POST'])
@login_required
def stop_container():
    """Stop a running container"""
    try:
        data = request.get_json()
        challenge_id = data.get('challenge_id')
        
        if not challenge_id:
            return jsonify({'success': False, 'error': 'Missing challenge_id'}), 400
        error = container_access_error(challenge_id)
        if error:
            return error
        
        result = container_orchestrator.stop_container(
            challenge_id=challenge_id,
            user_id=current_user.id
        )
        
        if result['success']:
            return jsonify(result), 200
        else:
            return jsonify(result), 400
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}'
        }), 500


@container_bp.route('/revert', methods=['POST'])
@login_required
def revert_container():
    """Revert (restart) a container"""
    try:
        data = request.get_json()
        challenge_id = data.get('challenge_id')
        
        if not challenge_id:
            return jsonify({'success': False, 'error': 'Missing challenge_id'}), 400
        error = container_access_error(challenge_id)
        if error:
            return error
        
        # Get user's team (if any)
        team = current_user.get_team()
        team_id = team.id if team else None
        
        result = hide_container_limit_details(container_orchestrator.revert_container(
            challenge_id=challenge_id,
            user_id=current_user.id,
            ip_address=request.remote_addr or '0.0.0.0',
            team_id=team_id
        ))
        
        if result['success']:
            return jsonify(result), 200
        else:
            return jsonify(result), 400
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}'
        }), 500


@container_bp.route('/status', methods=['GET'])
@login_required
def get_status():
    """Get status of user's container for a challenge"""
    try:
        challenge_id = request.args.get('challenge_id', type=int)
        
        if not challenge_id:
            return jsonify({'success': False, 'error': 'Missing challenge_id'}), 400
        error = container_access_error(challenge_id)
        if error:
            return error
        
        result = container_orchestrator.get_container_status(
            challenge_id=challenge_id,
            user_id=current_user.id
        )
        
        return jsonify(result), 200
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}'
        }), 500


@container_bp.route('/my-containers', methods=['GET'])
@login_required
def list_my_containers():
    """List all containers for current user"""
    try:
        containers = ContainerInstance.query.filter_by(
            user_id=current_user.id, team_id=current_user.team_id,
        ).filter(
            ContainerInstance.status.in_(['starting', 'running']),
            ContainerInstance.challenge_id.in_(visible_challenges_query(current_user).with_entities(Challenge.id)),
        ).all()
        
        result = []
        for container in containers:
            challenge = container.challenge
            connection_info = container_orchestrator._build_connection_info(
                challenge,
                container.host_ip,
                container.host_port
            ) if challenge and challenge.docker_connection_info else None
            
            result.append({
                **container.to_dict(),
                'challenge_name': challenge.name if challenge else 'Unknown',
                'connection_info': connection_info
            })
        
        return jsonify({
            'success': True,
            'containers': result
        }), 200
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}'
        }), 500


@container_bp.route('/force-cleanup', methods=['POST'])
@login_required
def force_cleanup():
    """
    Force cleanup of user's expired/stuck containers.
    Removes DB records and attempts to stop/remove Docker containers.
    """
    try:
        data = request.get_json() or {}
        challenge_id = data.get('challenge_id')
        
        # Find containers to clean up
        query = ContainerInstance.query.filter_by(user_id=current_user.id, team_id=current_user.team_id)
        
        if challenge_id:
            error = container_access_error(challenge_id)
            if error:
                return error
            query = query.filter_by(challenge_id=challenge_id)
        else:
            # Clean up expired/stopped containers only
            query = query.filter(
                db.or_(
                    ContainerInstance.status.in_(['stopped', 'error']),
                    ContainerInstance.expires_at < datetime.utcnow()
                ),
                ContainerInstance.challenge_id.in_(visible_challenges_query(current_user).with_entities(Challenge.id)),
            )
        
        containers = query.all()
        
        if not containers:
            return jsonify({
                'success': False,
                'error': 'No containers found to clean up'
            }), 404
        
        cleaned_count = 0
        errors = []
        
        # Ensure Docker client is available
        container_orchestrator._ensure_docker_client()
        
        for container in containers:
            try:
                # Try to stop/remove from Docker if exists
                if container_orchestrator.docker_client:
                    try:
                        docker_container = container_orchestrator.docker_client.containers.get(container.container_id)
                        if docker_container.status == 'running':
                            docker_container.stop(timeout=5)
                        docker_container.remove(force=True)
                    except Exception as docker_err:
                        # Container might already be gone, that's okay
                        pass
                
                # Remove from database
                db.session.delete(container)
                cleaned_count += 1
                
            except Exception as e:
                errors.append(f"Container {container.id}: {str(e)}")
        
        db.session.commit()
        
        if cleaned_count > 0:
            return jsonify({
                'success': True,
                'message': f'Cleaned up {cleaned_count} container(s)',
                'cleaned_count': cleaned_count,
                'errors': errors if errors else None
            }), 200
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to clean up containers',
                'errors': errors
            }), 500
    
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}'
        }), 500
