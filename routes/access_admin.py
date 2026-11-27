from flask import Blueprint, abort, current_app, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError

from models import db
from models.access_allowlist import AccessAllowlistEntry
from routes.admin import admin_required
from security_utils import CSRFProtection
from services import exclusive_access
from utils.audit import log_audit_event


access_admin_bp = Blueprint('access_admin', __name__, url_prefix='/admin')


def _allowlist_page():
    return db.paginate(
        db.select(AccessAllowlistEntry).order_by(AccessAllowlistEntry.email),
        page=request.args.get('access_page', 1, type=int),
        per_page=100,
        error_out=False,
    )


@access_admin_bp.app_context_processor
def inject_access_settings():
    if request.endpoint == 'admin.settings' and current_user.is_authenticated and current_user.is_admin:
        return {'exclusive_access_enabled': exclusive_access.is_enabled(), 'access_page': _allowlist_page()}
    return {}


@access_admin_bp.get('/exclusive-access')
@login_required
@admin_required
def list_allowlist():
    page = _allowlist_page()
    return jsonify(
        enabled=exclusive_access.is_enabled(),
        emails=[entry.email for entry in page.items],
        page=page.page,
        pages=page.pages,
        total=page.total,
    )


@access_admin_bp.post('/exclusive-access')
@login_required
@admin_required
@CSRFProtection.csrf_protect()
def update_allowlist():
    action = request.form.get('action')
    details = None
    try:
        if action in ('enable', 'disable'):
            exclusive_access.set_enabled(action == 'enable')
            message = f'Exclusive Access is {"on" if action == "enable" else "off"}.'
        elif action == 'clear':
            exclusive_access.clear_allowlist()
            message = 'The allowlist is empty.'
        elif action == 'upload':
            upload = request.files.get('allowlist_file')
            if not upload or not upload.filename:
                raise exclusive_access.AllowlistError('Choose an Excel (.xlsx) or CSV file to upload.')
            emails, invalid_count = exclusive_access.parse_allowlist(
                upload.filename, upload.stream.read(exclusive_access.MAX_UPLOAD_BYTES + 1)
            )
            if not emails:
                raise exclusive_access.AllowlistError('The file contains no valid email addresses.')
            keep_existing = request.form.get('keep_existing') == 'on'
            total = exclusive_access.replace_allowlist(emails, keep_existing=keep_existing)
            details = {'uploaded': len(emails), 'total': total, 'append': keep_existing}
            message = f'Loaded {len(emails):,} addresses. The allowlist now has {total:,} entries.'
            if invalid_count:
                message += f' Skipped {invalid_count:,} invalid email entries.'
        else:
            abort(400)
    except exclusive_access.AllowlistError as error:
        flash(str(error), 'error')
    except SQLAlchemyError as error:
        db.session.rollback()
        current_app.logger.warning('Exclusive Access %s failed (%s)', action, type(error).__name__)
        flash('The allowlist change could not be saved. Please retry.', 'error')
    else:
        log_audit_event(user_id=current_user.id, action=f'EXCLUSIVE_ACCESS_{action.upper()}', details=details)
        flash(message, 'success')
    return redirect(url_for('admin.settings', _anchor='exclusive-access'))
