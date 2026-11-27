"""Google OpenID Connect and verified account linking."""
import base64
from datetime import datetime
import hashlib
import re
import secrets
import time
from urllib.parse import urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from flask import current_app
from google.auth import jwt
import requests
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import db
from models.settings import Settings
from models.user import User
from services.auth_policy import GOOGLE_DOMAIN, NO_LOCAL_PASSWORD
from utils.identity import split_vit_name


AUTHORIZE_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
JWKS_URL = 'https://www.googleapis.com/oauth2/v3/certs'
ISSUERS = ('accounts.google.com', 'https://accounts.google.com')


class GoogleSignInError(ValueError):
    """A sign-in failure with a message safe to show to the user."""


def authorization_url(flow):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(flow['verifier'].encode()).digest())
    params = {
        'client_id': current_app.config['GOOGLE_CLIENT_ID'],
        'redirect_uri': current_app.config['GOOGLE_REDIRECT_URI'],
        'response_type': 'code',
        'scope': 'openid email profile',
        'hd': GOOGLE_DOMAIN,
        'state': flow['state'],
        'nonce': flow['nonce'],
        'prompt': 'select_account',
        'code_challenge_method': 'S256',
        'code_challenge': challenge.rstrip(b'=').decode(),
    }
    return f'{AUTHORIZE_URL}?{urlencode(params)}'


def _jwk_integer(value):
    return int.from_bytes(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)), 'big')


def exchange_id_token(code, flow):
    client_id = current_app.config['GOOGLE_CLIENT_ID']
    response = requests.post(TOKEN_URL, data={
        'code': code,
        'client_id': client_id,
        'client_secret': current_app.config['GOOGLE_CLIENT_SECRET'],
        'redirect_uri': current_app.config['GOOGLE_REDIRECT_URI'],
        'grant_type': 'authorization_code',
        'code_verifier': flow['verifier'],
    }, timeout=10, allow_redirects=False)
    response.raise_for_status()
    token = response.json()['id_token']
    header = jwt.decode_header(token)
    if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str) or not header['kid']:
        raise ValueError('Unexpected ID token signing algorithm or key')

    keys = requests.get(JWKS_URL, timeout=10, allow_redirects=False)
    keys.raise_for_status()
    key = next((key for key in keys.json()['keys'] if isinstance(key, dict) and key.get('kid') == header['kid']), None)
    if not key or key.get('kty') != 'RSA' or key.get('alg') != 'RS256' or key.get('use') != 'sig':
        raise ValueError('Unknown ID token signing key')
    public_key = RSAPublicNumbers(_jwk_integer(key['e']), _jwk_integer(key['n'])).public_key()
    pem = public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    claims = jwt.decode(token, certs=pem, audience=client_id)
    if type(claims['exp']) is not int or claims['exp'] <= time.time():
        raise ValueError('Invalid ID token expiry')
    if claims.get('iss') not in ISSUERS or claims.get('nonce') != flow['nonce']:
        raise ValueError('Invalid ID token issuer or nonce')
    if claims.get('azp', client_id) != client_id:
        raise ValueError('Invalid ID token authorized party')
    return claims


def verified_identity(claims):
    email = claims.get('email')
    subject = claims.get('sub')
    if (claims.get('email_verified') is not True or claims.get('hd') != GOOGLE_DOMAIN
            or not isinstance(subject, str) or not re.fullmatch(r'[\x21-\x7e]{1,255}', subject)
            or not isinstance(email, str) or len(email) > 120
            or not re.fullmatch(rf'[a-z0-9._%+\-]+@{re.escape(GOOGLE_DOMAIN)}', email, re.ASCII | re.IGNORECASE)):
        raise GoogleSignInError(f'Sign in with a verified Google Workspace account at @{GOOGLE_DOMAIN}.')
    return subject, email.lower()


def sign_in_account(subject, email, name, ip_address):
    name = name if isinstance(name, str) else ''
    display_name, roll_number = split_vit_name(name, email.split('@', 1)[0])
    for attempt in range(3):
        user = User.query.filter_by(google_sub=subject).first()
        # The post-filter also enforces an exact case-normalized match under
        # MySQL collations that equate accented and unaccented characters.
        email_matches = [candidate for candidate in User.query.filter(func.lower(User.email) == email)
                         if candidate.email.isascii() and candidate.email.lower() == email]
        existing = email_matches[0] if len(email_matches) == 1 else None
        if len(email_matches) > 1 or (existing and (
                existing.google_sub not in (None, subject) or (user and user.id != existing.id))):
            raise GoogleSignInError('This Google identity conflicts with an existing account. Contact an administrator.')

        user = user or existing
        try:
            if user is None:
                if not Settings.get('allow_registration', True, type='bool'):
                    raise GoogleSignInError('Registration is currently disabled. Please contact an administrator.')
                username = roll_number or email.split('@', 1)[0][:80]
                if attempt or User.query.filter(func.lower(User.username) == username.lower()).first():
                    username = f'{username[:67]}_{secrets.token_hex(6)}'
                user = User(username=username, password_hash=NO_LOCAL_PASSWORD, google_sub=subject, is_verified=True)
                db.session.add(user)
            elif not user.is_active:
                raise GoogleSignInError('Your account has been deactivated.')
            elif user.google_sub is None:
                if not user.is_verified:
                    raise GoogleSignInError('Verify your existing account by email before linking Google sign-in.')
                linked = User.query.filter_by(id=user.id, email=user.email, google_sub=None,
                                              is_verified=True, is_active=True).update({User.google_sub: subject})
                if not linked:
                    db.session.rollback()
                    continue

            user.email = email
            user.auth_provider = 'google'
            if name.strip() or not user.full_name:
                suffix = f' ({roll_number})' if roll_number else ''
                user.full_name = display_name[:120 - len(suffix)] + suffix
                user.roll_number = roll_number
            user.last_login = datetime.utcnow()
            user.last_ip_address = ip_address
            db.session.commit()
            return user
        except IntegrityError:
            db.session.rollback()

    raise GoogleSignInError('This Google account could not be linked. Please try again or contact an administrator.')
