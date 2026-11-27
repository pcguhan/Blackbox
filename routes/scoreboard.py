from flask import Blueprint, render_template, jsonify, flash, redirect, request, url_for
from flask_login import login_required, current_user
from models.settings import Settings
from services.cache import cache_service
from services.standings import frozen_snapshot, standing_index, standings_for, team_standings_enabled

scoreboard_bp = Blueprint('scoreboard', __name__, url_prefix='/scoreboard')

@scoreboard_bp.route('/')
@login_required
def view_scoreboard():
    """View scoreboard page"""
    
    # Check if CTF has started (admins bypass this check)
    is_admin = current_user.is_authenticated and current_user.is_admin
    if not is_admin and not Settings.is_ctf_started():
        start_time = Settings.get('ctf_start_time', type='datetime')
        return render_template('countdown.html', 
                             start_time=start_time,
                             page_title='Scoreboard',
                             return_url=url_for('scoreboard.view_scoreboard'))
    
    # Check if scoreboard is visible to users
    scoreboard_visible = Settings.get('scoreboard_visible', default=True, type='bool')
    
    # If scoreboard is hidden and user is not admin, redirect
    if not scoreboard_visible and (not current_user.is_authenticated or not current_user.is_admin):
        flash('The scoreboard is currently hidden by the admins.', 'info')
        return redirect(url_for('index'))
    
    # Check if teams are enabled
    teams_enabled = team_standings_enabled()
    
    return render_template('scoreboard.html', teams_enabled=teams_enabled, scoreboard_visible=scoreboard_visible)


def _selected_rows(rows, index):
    if request.args.get('nearby') == '1':
        return rows[max(0, index - 2):index + 3] if index is not None else []
    return rows[:100]


@scoreboard_bp.route('/api/data')
@login_required
def get_scoreboard_data():
    """Keep the existing array response for standings consumers."""
    rows = standings_for(current_user)
    return jsonify(_selected_rows(rows, standing_index(rows, current_user)))


@scoreboard_bp.route('/api/view')
@login_required
def get_scoreboard_view():
    """Return standings and the viewer's full-board rank in one response."""
    rows = standings_for(current_user)
    snapshot = frozen_snapshot()
    index = standing_index(rows, current_user)
    return jsonify(
        rows=_selected_rows(rows, index),
        me=rows[index] if index is not None else None,
        frozen_at=snapshot['at'] if snapshot else None,
        teams_enabled=team_standings_enabled(),
        visible=current_user.is_admin or Settings.get('scoreboard_visible', True, type='bool'),
        started=Settings.is_ctf_started(),
    )


@scoreboard_bp.route('/api/top/<int:limit>')
@login_required
def get_top_teams(limit):
    """Get the top teams or individual participants."""
    return jsonify(standings_for(current_user)[:min(limit, 100)])


@scoreboard_bp.route('/api/stats')
@login_required
def get_platform_stats():
    """Get overall platform statistics"""
    stats = cache_service.get_stats()
    
    if not stats:
        from models.user import User
        from models.team import Team
        from models.challenge import Challenge
        from models.submission import Submission, Solve
        
        stats = {
            'total_users': User.query.count(),
            'total_teams': Team.query.filter_by(is_active=True).count(),
            'total_challenges': Challenge.query.filter_by(is_visible=True, is_enabled=True).count(),
            'total_submissions': Submission.query.count(),
            'total_solves': Solve.participant_solves().filter(Solve.challenge_id.is_not(None)).count(),
            'challenges_by_category': {}
        }
        
        # Get challenges by category
        challenges = Challenge.query.filter_by(is_visible=True, is_enabled=True).all()
        for challenge in challenges:
            cat = challenge.category
            if cat not in stats['challenges_by_category']:
                stats['challenges_by_category'][cat] = 0
            stats['challenges_by_category'][cat] += 1
        
        cache_service.set_stats(stats, ttl=300)
    
    return jsonify(stats)
