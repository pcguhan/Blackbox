from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db
from models.hint import Hint, HintUnlock
from models.challenge import Challenge
from services.challenge_catalog import is_challenge_visible, participant_scope
from models.user import User
from models.team import Team
from services.cache import cache_service
from services.standings import publish_standings, team_standings_enabled

hints_bp = Blueprint('hints', __name__, url_prefix='/hints')


def hint_unlock_allowed(hint):
    prerequisite = hint.prerequisite_hint
    if prerequisite and not prerequisite.unlocks.filter(participant_scope(HintUnlock, current_user)).first():
        return False, f'You must unlock Hint #{prerequisite.order} first'
    return True, None


@hints_bp.route('/challenge/<int:challenge_id>', methods=['GET'])
@login_required
def get_challenge_hints(challenge_id):
    """Get hints for a challenge"""
    challenge = db.session.get(Challenge, challenge_id)

    if not is_challenge_visible(challenge, current_user):
        return jsonify(success=False, message='Challenge not found'), 404

    # Get user's team (needed for unlock checks)
    team = current_user.get_team() if team_standings_enabled() else None
    team_id = team.id if team else None

    hints = Hint.query.filter_by(challenge_id=challenge_id).order_by(Hint.order).all()
    
    hints_data = []
    for hint in hints:
        unlocked = hint.unlocks.filter(participant_scope(HintUnlock, current_user)).first() is not None
        can_unlock, reason = hint_unlock_allowed(hint)
        
        hint_data = {
            'id': hint.id,
            'cost': hint.cost,
            'order': hint.order,
            'unlocked': unlocked,
            'can_unlock': can_unlock or unlocked,  # If already unlocked, consider it "can unlock"
            'requires_hint_id': hint.requires_hint_id,
        }
        
        if not can_unlock and not unlocked:
            hint_data['locked_reason'] = reason
        
        if unlocked or current_user.is_admin:
            hint_data['content'] = hint.content
        
        hints_data.append(hint_data)
    
    return jsonify({'success': True, 'hints': hints_data})


@hints_bp.route('/<int:hint_id>/unlock', methods=['POST'])
@login_required
def unlock_hint(hint_id):
    """Unlock a hint by paying the cost"""
    if current_user.is_admin:
        return jsonify({'success': False, 'message': 'Admins can preview hints from the challenge page.'}), 403

    hint = db.session.get(Hint, hint_id)

    if not hint or not is_challenge_visible(hint.challenge, current_user):
        return jsonify(success=False, message='Challenge not found'), 404

    challenge = hint.challenge
    team = current_user.get_team() if team_standings_enabled() else None
    team_id = team.id if team else None

    # TOCTOU FIX: Lock the Hint row to serialize concurrent unlock attempts.
    # Without this lock, two team members clicking "unlock" simultaneously can
    # both pass the already_unlocked check, creating duplicate HintUnlock records
    # and deducting points twice.
    hint = Hint.query.filter_by(id=hint_id).with_for_update().first_or_404()
    
    # Check if already unlocked (now protected by the lock above)
    already_unlocked = hint.unlocks.filter(participant_scope(HintUnlock, current_user)).first() is not None
    
    if already_unlocked:
        return jsonify({'success': False, 'message': 'Hint already unlocked'}), 400
    
    # Check if prerequisites are met
    can_unlock, reason = hint_unlock_allowed(hint)
    if not can_unlock:
        return jsonify({'success': False, 'message': reason}), 400
    
    # Get current score
    if team:
        current_score = team.get_score()
    else:
        current_score = current_user.get_score()
    
    # Calculate new score (can go negative)
    new_score = current_score - hint.cost
    
    # Create hint unlock record
    try:
        hint_unlock = HintUnlock(
            hint_id=hint.id,
            user_id=current_user.id,
            team_id=team_id,
            cost_paid=hint.cost
        )
        db.session.add(hint_unlock)
        
        # Log hint unlock for tracking
        import logging
        logger = logging.getLogger('blackbox')
        
        if team:
            logger.info(f"HINT_UNLOCK: User '{current_user.username}' (ID: {current_user.id}) "
                       f"from Team '{team.name}' (ID: {team.id}) unlocked hint #{hint.order} "
                       f"for Challenge '{challenge.name}' (ID: {challenge.id}). "
                       f"Cost: {hint.cost} points. New score: {new_score}")
        else:
            logger.info(f"HINT_UNLOCK: User '{current_user.username}' (ID: {current_user.id}) "
                       f"unlocked hint #{hint.order} for Challenge '{challenge.name}' (ID: {challenge.id}). "
                       f"Cost: {hint.cost} points. New score: {new_score}")
        

        
        db.session.commit()
        cache_service.invalidate_scoring(challenge.id)
        publish_standings()

        return jsonify({
            'success': True,
            'message': f'Hint unlocked for {hint.cost} points',
            'content': hint.content,
            'new_score': new_score
        })
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


# Admin routes for managing hints
@hints_bp.route('/admin/challenge/<int:challenge_id>/hints', methods=['GET'])
@login_required
def admin_list_hints(challenge_id):
    """List all hints for a challenge (admin only)"""
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': 'Admin access required'}), 403
    
    challenge = Challenge.query.get_or_404(challenge_id)
    hints = Hint.query.filter_by(challenge_id=challenge_id).order_by(Hint.order).all()
    
    hints_data = [{'id': h.id, 'content': h.content, 'cost': h.cost, 'order': h.order} for h in hints]
    
    return jsonify({'success': True, 'hints': hints_data})


@hints_bp.route('/admin/challenge/<int:challenge_id>/hints', methods=['POST'])
@login_required
def admin_create_hint(challenge_id):
    """Create a new hint (admin only)"""
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': 'Admin access required'}), 403
    
    challenge = Challenge.query.get_or_404(challenge_id)
    
    content = request.form.get('content', '').strip()
    cost = request.form.get('cost', 0, type=int)
    order = request.form.get('order', 0, type=int)
    
    if not content:
        return jsonify({'success': False, 'message': 'Hint content is required'}), 400
    
    try:
        hint = Hint(
            challenge_id=challenge_id,
            content=content,
            cost=cost,
            order=order
        )
        db.session.add(hint)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Hint created successfully',
            'hint': hint.to_dict(include_content=True)
        })
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


@hints_bp.route('/admin/hints/<int:hint_id>', methods=['PUT', 'DELETE'])
@login_required
def admin_manage_hint(hint_id):
    """Update or delete a hint (admin only)"""
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': 'Admin access required'}), 403
    
    hint = Hint.query.get_or_404(hint_id)
    
    if request.method == 'DELETE':
        try:
            db.session.delete(hint)
            db.session.commit()
            cache_service.invalidate_scoring(hint.challenge_id)
            publish_standings()
            return jsonify({'success': True, 'message': 'Hint deleted successfully'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': str(e)}), 500
    
    content = request.form.get('content')
    cost = request.form.get('cost', type=int)
    order = request.form.get('order', type=int)
    
    try:
        if content:
            hint.content = content
        if cost is not None:
            hint.cost = cost
        if order is not None:
            hint.order = order
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Hint updated successfully',
            'hint': hint.to_dict(include_content=True)
        })
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
