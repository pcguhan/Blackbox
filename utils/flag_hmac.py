import base64
import hashlib
import hmac
import os
import re

DEFAULT_FLAG_PREFIX = 'CYS'
_SESSION_PATTERN = r'[0-9a-f]{32}'


def dynamic_flag_prefix(challenge):
    flag = challenge.flag or ''
    prefix, brace, _ = flag.partition('{')
    return (prefix if brace else '') or DEFAULT_FLAG_PREFIX


_PLACEHOLDER_SECRETS = {'', 'change-me-in-env', 'change-this-in-env', 'change-this-to-a-long-random-secret'}


def _master_secret():
    secret = os.environ.get('FLAG_HMAC_SECRET', '')
    if secret in _PLACEHOLDER_SECRETS:
        # A known secret lets anyone compute other teams' dynamic flags.
        if os.environ.get('FLASK_ENV', 'development') == 'production':
            raise RuntimeError('FLAG_HMAC_SECRET is missing or a placeholder; refusing to sign dynamic flags')
        secret = secret or 'change-me-in-env'
    return secret.encode()


def _challenge_key(challenge_id):
    secret = _master_secret()
    return hmac.digest(secret, f'challenge:{challenge_id}'.encode(), hashlib.sha256)


def generate_hmac_flag(challenge_id, team_id, user_id, prefix=DEFAULT_FLAG_PREFIX, *, session_id=None):
    if session_id:
        identifier = f'instance:{session_id}:user:{user_id}:team:{team_id}'
    else:
        identifier = f'team_{team_id}' if team_id else f'user_{user_id}'
    digest = hmac.digest(_challenge_key(challenge_id), identifier.encode(), hashlib.sha256).hex()
    return f'{prefix}{{{session_id}.{digest}}}' if session_id else f'{prefix}{{{digest}}}'


def verify_hmac_flag(submitted, challenge_id, team_id, user_id, prefix=DEFAULT_FLAG_PREFIX,
                     *, session_id=None, case_sensitive=True):
    expected = generate_hmac_flag(challenge_id, team_id, user_id, prefix, session_id=session_id)
    return flags_match(expected, submitted, case_sensitive)


def flags_match(expected, submitted, case_sensitive=True):
    submitted = submitted.strip()
    if not case_sensitive:
        expected, submitted = expected.lower(), submitted.lower()
    return hmac.compare_digest(expected.encode(), submitted.encode())


def instance_flag_session(flag):
    match = re.fullmatch(r'[^{}]+\{(' + _SESSION_PATTERN + r')\.[0-9a-f]{64}\}',
                         flag.strip(), re.IGNORECASE)
    return match[1].lower() if match else None


def is_hmac_flag(flag):
    return bool(instance_flag_session(flag) or re.fullmatch(
        r'[^{}]+\{[0-9a-fA-F]{64}\}', flag.strip(),
    ))


def is_instance_session(session_id):
    return re.fullmatch(_SESSION_PATTERN, session_id) is not None


def parse_legacy_flag(flag):
    """Extract the owner from a legacy flag for audit and stored-flag checks."""
    match = re.fullmatch(r'[^{}]+\{([a-zA-Z0-9_-]+={0,2})\}', flag.strip())
    if not match:
        return None
    payload = match[1]
    try:
        decoded = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode()
    except ValueError:
        return None
    parts = re.fullmatch(r'([0-9]{1,20}):(user|team)_([0-9]{1,20}):([^:]+)', decoded)
    if parts:
        challenge_id, scope, owner_id, date = parts.groups()
        is_team_flag = scope == 'team'
        return {
            'challenge_id': int(challenge_id), 'is_team_flag': is_team_flag, 'is_valid': True,
            'user_id': None if is_team_flag else int(owner_id),
            'team_id': int(owner_id) if is_team_flag else None,
            'date': None if date.isdigit() else date,
        }
