import secrets
import time

from flask import Blueprint, flash, redirect, request, session, url_for
from flask_login import current_user, login_user
from google.auth.exceptions import GoogleAuthError
from requests.exceptions import RequestException

from models.user import User
from services.auth_policy import SESSION_PROVIDER, google_available
from services.exclusive_access import BLOCKED_MESSAGE, enforce_for_user, is_email_allowed
from services.google_sign_in import (
    GoogleSignInError, authorization_url, exchange_id_token, sign_in_account, verified_identity,
)
from utils.audit import log_audit_event
from utils.identity import local_next


auth_google_bp = Blueprint('auth_google', __name__, url_prefix='/auth/google')
FLOW_KEY = 'google_oauth'
FLOW_LIFETIME = 600


@auth_google_bp.get('')
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if not google_available():
        flash('Google sign-in is unavailable. Please contact an administrator.', 'error')
        return redirect(url_for('auth.login'))

    flow = {
        'state': secrets.token_urlsafe(32),
        'nonce': secrets.token_urlsafe(32),
        'verifier': secrets.token_urlsafe(48),
        'created_at': time.time(),
        'next': local_next(request.args.get('next')),
    }
    session[FLOW_KEY] = flow
    return redirect(authorization_url(flow))


@auth_google_bp.get('/callback')
def callback():
    flow = session.pop(FLOW_KEY, None)
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if not google_available():
        flash('Google sign-in is unavailable. Please contact an administrator.', 'error')
        return redirect(url_for('auth.login'))

    state = request.args.get('state', '')
    code = request.args.get('code')
    if (not flow or not secrets.compare_digest(flow['state'].encode(), state.encode())
            or time.time() - flow['created_at'] > FLOW_LIFETIME or not code or request.args.get('error')):
        return _failed_sign_in('Google sign-in was interrupted or expired. Please try again.')

    try:
        claims = exchange_id_token(code, flow)
        subject, email = verified_identity(claims)
        # Exclusive Access applies before an account is created; existing admins stay exempt.
        existing = User.query.filter((User.google_sub == subject) | (User.email == email)).first()
        if not (existing and existing.is_admin) and not is_email_allowed(email):
            return _failed_sign_in(BLOCKED_MESSAGE)
        user = sign_in_account(subject, email, claims.get('name'), request.remote_addr)
        if not enforce_for_user(user):
            return _failed_sign_in(BLOCKED_MESSAGE)
    except GoogleSignInError as error:
        return _failed_sign_in(str(error))
    except (GoogleAuthError, RequestException, ValueError, KeyError, TypeError):
        return _failed_sign_in('Google sign-in could not be verified. Please try again.')

    session.clear()
    session['_remember'] = 'clear'
    if not login_user(user):
        return _failed_sign_in('Your account has been deactivated.')
    session[SESSION_PROVIDER] = 'google'
    log_audit_event(user_id=user.id, team_id=user.team_id, action='LOGIN_SUCCESS')
    destination = 'admin.dashboard' if user.is_admin else 'challenges.list_challenges'
    return redirect(local_next(flow['next']) or url_for(destination))


def _failed_sign_in(message):
    log_audit_event(action='LOGIN_FAILED')
    flash(message, 'error')
    return redirect(url_for('auth.login'))
