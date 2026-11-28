"""Profile settings for authenticated users."""

from pathlib import Path
import re
from uuid import uuid4

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError

from models import db
from models.user import User
from security_utils import CSRFProtection
from services.image_sanitizer import IMAGE_MIMETYPES, ImageTooLarge, InvalidImage, sanitize_upload


profile_settings_bp = Blueprint('profile_settings', __name__)
AVATAR_NAME = re.compile(r'[0-9a-f]{32}\.(png|jpg|webp)')


def _delete_avatar(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        current_app.logger.warning('Could not delete avatar %s', path.name, exc_info=True)


def _profile_data():
    return {
        'username': current_user.username,
        'full_name': current_user.full_name,
        'nickname': current_user.nickname,
        'auth_provider': getattr(current_user, 'auth_provider', None) or 'local',
        'roll_number': getattr(current_user, 'roll_number', None),
        'avatar_url': url_for('.serve_avatar', filename=current_user.avatar_filename) if current_user.avatar_filename else None,
    }


def _update_profile():
    updates = {}
    if 'nickname' in request.form and getattr(current_user, 'auth_provider', None) != 'google':
        nickname = request.form['nickname'].strip()
        if len(nickname) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in nickname):
            return 'Enter a nickname of up to 80 characters on one line.', 400
        updates['nickname'] = nickname or None

    upload = request.files.get('avatar')
    remove = request.form.get('remove_avatar') in ('1', 'true', 'on')
    folder = Path(current_app.config['AVATAR_UPLOAD_FOLDER']).resolve()
    previous = current_user.avatar_filename
    new_path = None
    if upload and upload.filename:
        if remove:
            return 'Choose a new picture or remove the current picture.', 400
        try:
            data, extension = sanitize_upload(upload, max_bytes=current_app.config['AVATAR_MAX_BYTES'])
        except InvalidImage as error:
            return str(error), 413 if isinstance(error, ImageTooLarge) else 400
        path = folder / f'{uuid4().hex}.{extension}'
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as target:
                new_path = path
                target.write(data)
        except OSError:
            if new_path:
                _delete_avatar(new_path)
            current_app.logger.exception('Could not store avatar')
            return 'The picture could not be saved.', 500
        updates['avatar_filename'] = path.name
    elif remove:
        updates['avatar_filename'] = None

    if updates:
        statement = db.update(User).where(User.id == current_user.id)
        if 'avatar_filename' in updates:
            statement = statement.where(User.avatar_filename == previous)
        try:
            if not db.session.execute(statement.values(**updates)).rowcount:
                db.session.rollback()
                if new_path:
                    _delete_avatar(new_path)
                return 'The profile changed in another request. Reload and try again.', 409
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            if new_path:
                _delete_avatar(new_path)
            raise
        if previous and 'avatar_filename' in updates and AVATAR_NAME.fullmatch(previous):
            _delete_avatar(folder / previous)

    return 'Profile settings saved.', 200


@profile_settings_bp.route('/profile/settings', methods=['GET', 'POST'])
@login_required
@CSRFProtection.csrf_protect()
def settings_page():
    status = 200
    if request.method == 'POST':
        message, status = _update_profile()
        flash(message, 'success' if status == 200 else 'error')
        if status == 200:
            return redirect(url_for('.settings_page'))
    return render_template('profile_settings.html', profile=_profile_data()), status


@profile_settings_bp.route('/api/profile/settings', methods=['GET', 'POST'])
@login_required
@CSRFProtection.csrf_protect()
def settings_api():
    if request.method == 'POST':
        message, status = _update_profile()
        if status != 200:
            return jsonify(message=message), status
    response = jsonify(_profile_data())
    response.cache_control.no_store = True
    return response


@profile_settings_bp.get('/avatars/<filename>')
@login_required
def serve_avatar(filename):
    match = AVATAR_NAME.fullmatch(filename)
    if not match:
        abort(404)
    folder = Path(current_app.config['AVATAR_UPLOAD_FOLDER']).resolve()
    if (folder / filename).is_symlink():
        abort(404)
    response = send_from_directory(folder, filename, mimetype=IMAGE_MIMETYPES[match[1]])
    response.headers['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; sandbox"
    response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    response.cache_control.private = True
    return response
