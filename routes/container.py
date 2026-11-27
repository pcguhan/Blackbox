"""
Container Management Routes
Endpoints for starting, stopping, and managing challenge containers
"""

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from services.container_manager import container_orchestrator

container_bp = Blueprint('container', __name__, url_prefix='/container')

@container_bp.route('/start', methods=['POST'])
@login_required
def start_container():
    """Start a Docker container for a challenge"""
    try:
        data = request.get_json(silent=True) or {}
        challenge_id = data.get('challenge_id')
        
        if type(challenge_id) is not int or challenge_id < 1:
            return jsonify({'success': False, 'error': 'Missing challenge_id'}), 400
        
        # Get user's team (if any)
        team = current_user.get_team()
        team_id = team.id if team else None
        
        # Start container
        result = container_orchestrator.start_container(
            challenge_id=challenge_id,
            user_id=current_user.id,
            ip_address=request.remote_addr or '0.0.0.0',
            team_id=team_id
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


@container_bp.route('/stop', methods=['POST'])
@login_required
def stop_container():
    """Stop a running container"""
    try:
        data = request.get_json(silent=True) or {}
        challenge_id = data.get('challenge_id')
        
        if type(challenge_id) is not int or challenge_id < 1:
            return jsonify({'success': False, 'error': 'Missing challenge_id'}), 400
        
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
        
        # Get user's team (if any)
        team = current_user.get_team()
        team_id = team.id if team else None
        
        result = container_orchestrator.revert_container(
            challenge_id=challenge_id,
            user_id=current_user.id,
            ip_address=request.remote_addr or '0.0.0.0',
            team_id=team_id
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


@container_bp.route('/status', methods=['GET'])
@login_required
def get_status():
    """Get status of user's container for a challenge"""
    try:
        challenge_id = request.args.get('challenge_id', type=int)
        
        if type(challenge_id) is not int or challenge_id < 1:
            return jsonify({'success': False, 'error': 'Missing challenge_id'}), 400
        
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
        containers = container_orchestrator._active_instances().filter_by(user_id=current_user.id).all()
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

    result = container_orchestrator.force_cleanup(current_user.id, challenge_id)
    if result['success']:
        return jsonify(result), 200
    return jsonify(result), 404 if result.get('not_found') else 400
