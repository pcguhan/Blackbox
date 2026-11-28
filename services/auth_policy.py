"""Authentication settings and the Google-only session policy."""
from flask import current_app, g, session

from models.settings import Settings


GOOGLE_ENABLED = 'auth_google_enabled'
GOOGLE_ONLY = 'auth_google_only'
SESSION_PROVIDER = 'auth_provider'
GOOGLE_ONLY_MESSAGE = 'Participants must sign in with Google.'
GOOGLE_DOMAIN = 'vitstudent.ac.in'
NO_LOCAL_PASSWORD = '!'


def _policy():
    # Read both controls together from the DB so a cache invalidation cannot
    # temporarily allow password sessions or a disabled provider.
    if 'auth_policy' not in g:
        settings = Settings.query.filter(Settings.key.in_((GOOGLE_ENABLED, GOOGLE_ONLY))).all()
        g.auth_policy = {setting.key: Settings._convert_value(setting.value, 'bool', False) for setting in settings}
    return g.auth_policy


def google_only():
    return _policy().get(GOOGLE_ONLY, False)


def google_enabled():
    return _policy().get(GOOGLE_ENABLED, bool(current_app.config.get('GOOGLE_CLIENT_ID')))


def google_configured():
    secret = current_app.secret_key
    if not current_app.testing and (
        not secret or len(secret) < 32 or secret.startswith(('dev-secret-key', 'change-this'))
    ):
        return False
    return all(current_app.config.get(key) for key in (
        'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REDIRECT_URI',
    ))


def google_available():
    return google_configured() and google_enabled()


def local_session_blocked(user):
    return not user.is_admin and session.get(SESSION_PROVIDER) != 'google' and google_only()
