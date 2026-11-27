"""Administrative technology inventory endpoint."""

from flask import Blueprint, jsonify
from flask_login import current_user

from services.version_inventory import collect_versions


admin_versions_bp = Blueprint('admin_versions', __name__, url_prefix='/admin')


@admin_versions_bp.get('/api/versions')
def versions():
    if not current_user.is_authenticated:
        response = jsonify(error='Sign in to continue.')
        response.status_code = 401
    elif not current_user.is_admin:
        response = jsonify(error='Admin access required.')
        response.status_code = 403
    else:
        response = jsonify(collect_versions())
    response.cache_control.no_store = True
    return response
