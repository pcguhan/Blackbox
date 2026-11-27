from flask import Blueprint, flash, redirect, request, url_for
from flask_login import login_required
from sqlalchemy.exc import IntegrityError, OperationalError

from models import db
from models.settings import Settings
from routes.admin import admin_required
from security_utils import CSRFProtection
from services.auth_policy import GOOGLE_ENABLED, GOOGLE_ONLY, google_configured, google_enabled, google_only


auth_admin_bp = Blueprint('auth_admin', __name__, url_prefix='/admin/settings')


@auth_admin_bp.app_template_global()
def auth_provider_settings():
    return dict(configured=google_configured(), enabled=google_enabled(), only=google_only())


@auth_admin_bp.post('/auth')
@login_required
@admin_required
@CSRFProtection.csrf_protect()
def update():
    enabled = 'auth_google_enabled' in request.form
    only = 'auth_google_only' in request.form
    if only and not (enabled and google_configured()):
        flash('Enable and configure Google sign-in before requiring it for participants.', 'error')
        return redirect(url_for('admin.settings'))

    try:
        rows = Settings.query.filter(Settings.key.in_((GOOGLE_ENABLED, GOOGLE_ONLY))).with_for_update().all()
        settings = {row.key: row for row in rows}
        for key, value in ((GOOGLE_ENABLED, enabled), (GOOGLE_ONLY, only)):
            setting = settings.get(key)
            if setting is None:
                setting = Settings(key=key)
                db.session.add(setting)
            setting.value = 'true' if value else 'false'
            setting.value_type = 'bool'
        db.session.commit()
    except (IntegrityError, OperationalError):
        db.session.rollback()
        flash('Authentication settings could not be saved. Please try again.', 'error')
        return redirect(url_for('admin.settings'))

    Settings.clear_cache(GOOGLE_ENABLED)
    Settings.clear_cache(GOOGLE_ONLY)
    flash('Authentication settings saved.', 'success')
    return redirect(url_for('admin.settings'))
