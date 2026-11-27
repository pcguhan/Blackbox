import re

from flask import Blueprint, current_app, flash, redirect, request, url_for
from flask_login import login_required
from sqlalchemy.exc import SQLAlchemyError

from models import db
from models.settings import Settings
from routes.admin import admin_required


appearance_admin_bp = Blueprint('appearance_admin', __name__, url_prefix='/admin')

# Preserve the existing checks for custom CSS and keep it inside the style element.
UNSAFE_BACKGROUND_CSS = re.compile(
    r'<script|javascript:|onerror|onload|eval\(|expression\('
    r'|import\s+["\']|behavior:|binding:|-moz-binding',
    re.IGNORECASE,
)
STYLE_END = re.compile(r'</style\b', re.IGNORECASE)


@appearance_admin_bp.app_context_processor
def inject_appearance():
    return {
        'site_palette': Settings.site_palette(),
        'site_font': Settings.site_font(),
        'site_palettes': Settings.SITE_PALETTES,
        'site_fonts': Settings.SITE_FONTS,
    }


@appearance_admin_bp.post('/settings/appearance')
@login_required
@admin_required
def update_appearance():
    settings_url = url_for('admin.settings')
    updates = {}

    if 'site_theme' in request.form:
        palette = request.form['site_theme']
        if palette not in Settings.SITE_PALETTES:
            flash('Choose one of the available colour palettes.', 'error')
            return redirect(settings_url)
        updates['site_theme'] = (palette, 'string', 'Site colour palette selected by administrators')

    if 'site_font' in request.form:
        font = request.form['site_font']
        if font not in Settings.SITE_FONTS:
            flash('Choose one of the available fonts.', 'error')
            return redirect(settings_url)
        updates['site_font'] = (font, 'string', 'Site font selected by administrators')

    if ('custom_background_css' in request.form or 'custom_background_enabled' in request.form
            or request.endpoint == 'admin.update_background_theme'):
        enabled = 'custom_background_enabled' in request.form
        updates['custom_background_enabled'] = (enabled, 'bool', 'Enable custom background theme')
        if enabled:
            css = request.form.get('custom_background_css', '').strip()
            css_clean = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)
            if UNSAFE_BACKGROUND_CSS.search(css_clean) or STYLE_END.search(css):
                flash('Invalid CSS: Potentially dangerous content detected', 'error')
                return redirect(settings_url)
            updates['custom_background_css'] = (css, 'string', 'Custom background CSS')

    try:
        Settings.set_many(updates)
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Could not save appearance settings')
        flash('Appearance settings could not be saved.', 'error')
    else:
        flash('Appearance settings saved.', 'success')
    return redirect(settings_url)
