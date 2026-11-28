from flask import Blueprint, request
from flask_login import login_required

from routes.admin import admin_required
from services.admin_console import (
    active_containers_query,
    csv_download,
    hint_log_rows,
    hint_unlocks_query,
    users_query,
)
from utils.timezone import format_datetime


adminui_bp = Blueprint('adminui', __name__, url_prefix='/admin')


@adminui_bp.get('/users/export')
@login_required
@admin_required
def export_users():
    rows = (
        (
            user.id, user.username, user.full_name, user.email, user.team.name if user.team else '',
            user.get_score(), user.get_solves_count(), 'Admin' if user.is_admin else 'Player',
            'Active' if user.is_active else 'Disabled', format_datetime(user.created_at),
            format_datetime(user.last_login) if user.last_login else '',
        )
        for user in users_query(request.args)
    )
    return csv_download('users', (
        'ID', 'Username', 'Full Name', 'Email', 'Team', 'Score', 'Solves', 'Role', 'Status',
        'Created At', 'Last Login',
    ), rows)


@adminui_bp.get('/hint-logs/export')
@login_required
@admin_required
def export_hint_logs():
    rows = (
        (
            unlock.id, format_datetime(unlock.unlocked_at),
            unlock.user.username if unlock.user else unlock.user_id,
            unlock.team.name if unlock.team else '',
            unlock.hint.challenge.name if unlock.hint and unlock.hint.challenge else '',
            unlock.hint.order if unlock.hint else '', unlock.cost_paid,
        )
        for unlock in hint_log_rows(hint_unlocks_query(request.args))
    )
    return csv_download('hint-logs', ('ID', 'Time', 'User', 'Team', 'Challenge', 'Hint', 'Cost'), rows)


@adminui_bp.get('/dynamic-flags/export')
@login_required
@admin_required
def export_dynamic_flags():
    rows = (
        (
            container.id, container.challenge.name if container.challenge else container.challenge_id,
            container.team.name if container.team else '',
            container.user.username if container.user else container.user_id, container.status,
            container.get_expected_flag(), format_datetime(container.created_at),
            format_datetime(container.expires_at),
        )
        for container in active_containers_query(request.args)
    )
    return csv_download('dynamic-flags', (
        'ID', 'Challenge', 'Team', 'User', 'Status', 'Dynamic Flag', 'Created At', 'Expires At',
    ), rows)
