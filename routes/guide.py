from flask import Blueprint, render_template

from models.settings import Settings


guide_bp = Blueprint('guide', __name__)


@guide_bp.get('/guide')
def view_guide():
    return render_template(
        'guide.html',
        google_only=Settings.get('auth_google_only', default=False, type='bool'),
        flag_prefix=Settings.get('flag_format_prefix', default='CYS') or 'CYS',
        start_time=Settings.get('ctf_start_time', type='datetime'),
        end_time=Settings.get('ctf_end_time', type='datetime'),
    )
