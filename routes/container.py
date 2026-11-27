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
        data = request.get_json(silent=True) or {}
        challenge_id = data.get('challenge_id')
        
        if type(challenge_id) is not int or challenge_id < 1:
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
        data = request.get_json(silent=True) or {}
        challenge_id = data.get('challenge_id')
        
        if type(challenge_id) is not int or challenge_id < 1:
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
        data = request.get_json(silent=True) or {}
        challenge_id = data.get('challenge_id')
        
        if type(challenge_id) is not int or challenge_id < 1:
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
        
        if type(challenge_id) is not int or challenge_id < 1:
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
        containers = container_orchestrator._active_instances().filter_by(user_id=current_user.id, team_id=current_user.team_id).filter(
            ContainerInstance.challenge_id.in_(visible_challenges_query(current_user).with_entities(Challenge.id))
        ).all()
        result = [
            {**container_orchestrator._instance_data(instance), 'challenge_name': instance.challenge.name}
            for instance in containers
        ]

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
    """Retry removal of the current user's expired or stuck instances."""
    data = request.get_json(silent=True) or {}
    challenge_id = data.get('challenge_id')
    if challenge_id is not None and (type(challenge_id) is not int or challenge_id < 1):
        return jsonify({'success': False, 'error': 'Invalid challenge_id'}), 400
    if challenge_id is not None:
        error = container_access_error(challenge_id)
        if error:
            return error

    result = container_orchestrator.force_cleanup(current_user.id, challenge_id)
    if result['success']:
        return jsonify(result), 200
    return jsonify(result), 404 if result.get('not_found') else 400
