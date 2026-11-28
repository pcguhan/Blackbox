from flask import Blueprint, render_template, redirect, url_for, flash, jsonify, request, session
from flask_login import login_user, logout_user, login_required, current_user
from models import db
from models.user import User
from datetime import datetime
from utils.audit import log_audit_event
from utils.identity import local_next
import re
from utils.email import (send_email, generate_confirmation_token, verify_token, EMAIL_CONFIRM_SALT,
                         generate_password_reset_token, verify_password_reset_token, email_configured)
from flask import current_app
from services.exclusive_access import BLOCKED_MESSAGE, DEACTIVATED_MESSAGE, enforce_for_user, is_email_allowed
from services.auth_policy import (
    GOOGLE_ONLY_MESSAGE, NO_LOCAL_PASSWORD, SESSION_PROVIDER, google_available, google_only, local_session_blocked,
)

auth_bp = Blueprint('auth', __name__)

# In-app throttling, so login is protected however the app is reached (Nginx,
# Caddy, or a direct port). Per-IP mirrors Nginx's 20 r/m login zone;
# per-username failures stop distributed guessing against one account.
LOGIN_IP_LIMIT, LOGIN_IP_WINDOW = 20, 60
LOGIN_FAIL_LIMIT, LOGIN_FAIL_WINDOW = 10, 900
EMAIL_ACTION_LIMIT, EMAIL_ACTION_WINDOW = 5, 900


def _throttled(key, limit, window):
    """True if ``key`` exceeded ``limit`` hits in ``window`` seconds. Fails open if Redis is down."""
    from services.cache import cache_service
    try:
        allowed, _ = cache_service.check_rate_limit(key, limit=limit, window=window)
        return not allowed
    except Exception as exc:
        current_app.logger.warning(f"Rate limiter unavailable ({exc}); allowing request")
        return False


def _login_fail_key(username):
    return f'login_fail:{username.lower()}'


def _login_failures_exceeded(username):
    from services.cache import cache_service
    try:
        return int(cache_service.redis_client.get(_login_fail_key(username)) or 0) >= LOGIN_FAIL_LIMIT
    except Exception:
        return False


def _record_login_failure(username):
    from services.cache import cache_service
    try:
        pipe = cache_service.redis_client.pipeline()
        pipe.incr(_login_fail_key(username))
        pipe.expire(_login_fail_key(username), LOGIN_FAIL_WINDOW)
        pipe.execute()
    except Exception:
        pass


def _clear_login_failures(username):
    from services.cache import cache_service
    try:
        cache_service.redis_client.delete(_login_fail_key(username))
    except Exception:
        pass


@auth_bp.before_app_request
def enforce_google_only():
    if request.endpoint in {'static', 'health_check'}:
        return
    if current_user.is_authenticated and local_session_blocked(current_user):
        logout_user()
        session.pop(SESSION_PROVIDER, None)
        flash(GOOGLE_ONLY_MESSAGE, 'error')
        if request.path.startswith('/api/') or request.is_json:
            return jsonify(success=False, message=GOOGLE_ONLY_MESSAGE), 403
        return redirect(url_for('auth.login'))


def _google_only_redirect():
    flash(GOOGLE_ONLY_MESSAGE, 'error')
    return redirect(url_for('auth.login'))


def _login_page(unverified_email=None):
    return render_template('login.html', google_enabled=google_available(), google_only=google_only(),
                           next_page=local_next(request.args.get('next')), unverified_email=unverified_email)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page and handler"""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = request.form.get('remember', False)
        
        if not username or not password:
            flash('Please fill in all fields', 'error')
            return _login_page()

        if _throttled(f'login_ip:{request.remote_addr}', LOGIN_IP_LIMIT, LOGIN_IP_WINDOW) or _login_failures_exceeded(username):
            log_audit_event(action='LOGIN_THROTTLED', details={'username': username})
            flash('Too many login attempts. Please wait a few minutes and try again.', 'error')
            return _login_page(), 429

        user = User.query.filter_by(username=username).first()

        if user and user.password_hash != NO_LOCAL_PASSWORD and user.check_password(password):
            _clear_login_failures(username)
            if google_only() and not user.is_admin:
                flash(GOOGLE_ONLY_MESSAGE, 'error')
                return _login_page()
            if not enforce_for_user(user):
                flash(BLOCKED_MESSAGE if user.is_active else DEACTIVATED_MESSAGE, 'error')
                return _login_page()
                
            from models.settings import Settings
            require_verification = Settings.get('require_email_verification', True)
            
            if require_verification and not getattr(user, 'is_verified', True):
                flash('Please verify your email address before logging in. Check your inbox.', 'error')
                return _login_page(unverified_email=user.email)
            
            login_user(user, remember=remember)
            session[SESSION_PROVIDER] = 'local'
            user.last_login = datetime.utcnow()
            user.last_ip_address = request.remote_addr
            db.session.commit()
            
            log_audit_event(user_id=user.id, team_id=user.team_id, action='LOGIN_SUCCESS')
            
            next_page = local_next(request.args.get('next'))
            if next_page:
                return redirect(next_page)
            
            # Redirect based on admin status
            if user.is_admin:
                return redirect(url_for('admin.dashboard'))
            else:
                return redirect(url_for('challenges.list_challenges'))
        else:
            _record_login_failure(username)
            log_audit_event(action='LOGIN_FAILED', details={'username': username})
            flash('Invalid username or password', 'error')
    
    return _login_page()


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """Registration page and handler"""
    if google_only():
        return _google_only_redirect()
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    # Check if registration is enabled (check database settings first, then config)
    from models.settings import Settings
    registration_enabled = Settings.get('allow_registration', True)
    
    if not registration_enabled:
        flash('Registration is currently disabled. Please contact an administrator.', 'error')
        return redirect(url_for('auth.login'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        full_name = request.form.get('full_name')
        
        # Validation
        if not all([username, email, password, confirm_password]):
            flash('Please fill in all required fields', 'error')
            return render_template('register.html')
            
        # Validate username (alphanumeric and underscore only, 3-30 chars)
        if not re.match(r'^[a-zA-Z0-9_]{3,30}$', username):
            flash('Username must be 3-30 characters long and contain only letters, numbers, and underscores', 'error')
            return render_template('register.html')
            
        # Validate email format
        if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
            flash('Please enter a valid email address', 'error')
            return render_template('register.html')
        if not is_email_allowed(email):
            flash(BLOCKED_MESSAGE, 'error')
            return render_template('register.html')
        
        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return render_template('register.html')
        
        if len(password) < 6:
            flash('Password must be at least 6 characters long', 'error')
            return render_template('register.html')
        
        # Check if user already exists
        if User.query.filter_by(username=username).first():
            flash('Username already taken', 'error')
            return render_template('register.html')
        
        if User.query.filter_by(email=email).first():
            flash('Email already registered', 'error')
            return render_template('register.html')
        
        # Create new user
        user = User(
            username=username,
            email=email,
            full_name=full_name
        )
        user.set_password(password)
        
        db.session.add(user)
        # TOCTOU FIX: Two concurrent registrations with the same username/email
        # can both pass the existence checks above. Catch IntegrityError from the
        # DB UNIQUE constraint and return a friendly message instead of a 500.
        try:
            db.session.commit()
        except Exception as commit_err:
            db.session.rollback()
            err_msg = str(commit_err).lower()
            if 'unique' in err_msg or 'duplicate' in err_msg or 'integrity' in err_msg:
                flash('Username or email already taken. Please try a different one.', 'error')
                return render_template('register.html')
            raise
        
        # Send verification email if required
        from models.settings import Settings
        require_verification = Settings.get('require_email_verification', True)
        if require_verification and not email_configured():
            current_app.logger.error("Verification email not sent: SMTP is not configured")
            flash('Registration successful, but this server cannot send email yet, so no verification '
                  'email was sent. Please contact an administrator to verify your account.', 'warning')
        elif require_verification:
            try:
                token = generate_confirmation_token(user.email)
                base_url = Settings.get('base_url', '')
                if base_url:
                    verify_url = f"{base_url.rstrip('/')}{url_for('auth.verify_email', token=token)}"
                else:
                    verify_url = url_for('auth.verify_email', token=token, _external=True)
                    
                html = f'<p>Welcome to {current_app.config.get("CTF_NAME", "the CTF")}!</p><p>Please verify your email by clicking the link below:</p><p><a href="{verify_url}" style="display:inline-block;padding:10px 20px;background-color:#667eea;color:white;text-decoration:none;border-radius:5px;font-weight:bold;">Verify Email Address</a></p>'
                
                import gevent
                from utils.email import send_email_async
                app = current_app._get_current_object()
                gevent.spawn(send_email_async, app, user.email, 'Verify your email address', html)
                flash('Registration successful! A verification email has been sent to your address. Please verify before logging in.', 'success')
            except Exception as e:
                current_app.logger.error(f"Error preparing verification email: {e}")
                flash('Registration successful, but there was an unexpected error preparing the email system.', 'warning')
        else:
            flash('Registration successful! Please login.', 'success')
        
        log_audit_event(user_id=user.id, action='REGISTER')
        
        # Clear any stale session cookies. This prevents an issue where testing with 
        # database resets causes old session cookies to instantly log the user in 
        # (since the new user gets the same database ID as the old deleted one).
        logout_user()
        
        return redirect(url_for('auth.login'))
    
    return render_template('register.html')


@auth_bp.route('/logout')
@login_required
def logout():
    """Logout handler"""
    _user_id = current_user.id
    _team_id = current_user.team_id
    logout_user()
    session.pop(SESSION_PROVIDER, None)
    log_audit_event(user_id=_user_id, team_id=_team_id, action='LOGOUT')
    flash('You have been logged out', 'info')
    return redirect(url_for('index'))


@auth_bp.route('/profile')
@login_required
def profile():
    """User profile page"""
    from services.scoring import ScoringService
    
    progress = ScoringService.get_user_progress(current_user.id)
    
    return render_template('profile.html', user=current_user, progress=progress)


@auth_bp.route('/verify-email/<token>')
def verify_email(token):
    email = verify_token(token, salt=EMAIL_CONFIRM_SALT)
    if not email:
        flash('The verification link is invalid or has expired.', 'error')
        return redirect(url_for('auth.login'))
        
    user = User.query.filter_by(email=email).first()
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('auth.login'))
        
    if getattr(user, 'is_verified', False):
        flash('Account already verified. Please login.', 'success')
    else:
        user.is_verified = True
        db.session.commit()
        log_audit_event(user_id=user.id, action='EMAIL_VERIFIED')
        flash('You have successfully verified your email! You can now login.', 'success')
        
    return redirect(url_for('auth.login'))


@auth_bp.route('/resend-verification', methods=['POST'])
def resend_verification():
    if google_only():
        return _google_only_redirect()
    email = request.form.get('email')
    if _throttled(f'email_action_ip:{request.remote_addr}', EMAIL_ACTION_LIMIT, EMAIL_ACTION_WINDOW):
        flash('Too many requests. Please wait a few minutes and try again.', 'error')
        return redirect(url_for('auth.login'))
    user = User.query.filter_by(email=email).first()
    if user and not getattr(user, 'is_verified', True) and not email_configured():
        flash('This server cannot send email yet. Please contact an administrator to verify your account.', 'warning')
    elif user and not getattr(user, 'is_verified', True):
        token = generate_confirmation_token(user.email)
        from models.settings import Settings
        base_url = Settings.get('base_url', '')
        if base_url:
            verify_url = f"{base_url.rstrip('/')}{url_for('auth.verify_email', token=token)}"
        else:
            verify_url = url_for('auth.verify_email', token=token, _external=True)
            
        html = f'<p>Please verify your email by clicking the link below:</p><p><a href="{verify_url}" style="display:inline-block;padding:10px 20px;background-color:#667eea;color:white;text-decoration:none;border-radius:5px;font-weight:bold;">Verify Email Address</a></p>'
        
        import gevent
        from utils.email import send_email_async
        app = current_app._get_current_object()
        gevent.spawn(send_email_async, app, user.email, 'Verify your email address', html)
        flash('Verification email resent. Please check your inbox.', 'success')
    else:
        flash('Invalid request or user already verified.', 'error')
    return redirect(url_for('auth.login'))


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if google_only():
        return _google_only_redirect()
    if current_user.is_authenticated:
        return redirect(url_for('index'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        if _throttled(f'email_action_ip:{request.remote_addr}', EMAIL_ACTION_LIMIT, EMAIL_ACTION_WINDOW):
            flash('Too many requests. Please wait a few minutes and try again.', 'error')
            return redirect(url_for('auth.login'))
        user = User.query.filter_by(email=email).first()
        if user and user.password_hash != NO_LOCAL_PASSWORD and not email_configured():
            current_app.logger.error("Password reset email not sent: SMTP is not configured")
        elif user and user.password_hash != NO_LOCAL_PASSWORD:
            token = generate_password_reset_token(user)
            from models.settings import Settings
            base_url = Settings.get('base_url', '')
            if base_url:
                reset_url = f"{base_url.rstrip('/')}{url_for('auth.reset_password', token=token)}"
            else:
                reset_url = url_for('auth.reset_password', token=token, _external=True)
                
            html = f'<p>To reset your password, click the link below:</p><p><a href="{reset_url}" style="display:inline-block;padding:10px 20px;background-color:#667eea;color:white;text-decoration:none;border-radius:5px;font-weight:bold;">Reset Password</a></p>'
            import gevent
            from utils.email import send_email_async
            app = current_app._get_current_object()
            gevent.spawn(send_email_async, app, user.email, 'Password Reset Request', html)
        
        flash('If an account exists with that email, a password reset link has been sent.', 'info')
        return redirect(url_for('auth.login'))
        
    return render_template('forgot_password.html')


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if google_only():
        return _google_only_redirect()
    if current_user.is_authenticated:
        return redirect(url_for('index'))
        
    # Reset tokens use their own salt (a verification token is rejected here)
    # and embed a password fingerprint, so each one works only once.
    user = verify_password_reset_token(token)
    if not user:
        flash('The password reset link is invalid, expired or has already been used.', 'error')
        return redirect(url_for('auth.login'))

    if user.password_hash == NO_LOCAL_PASSWORD:
        flash('This account signs in with Google.', 'info')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if not password or password != confirm_password:
            flash('Passwords do not match or are empty', 'error')
            return render_template('reset_password.html', token=token)
            
        if len(password) < 6:
            flash('Password must be at least 6 characters long', 'error')
            return render_template('reset_password.html', token=token)
            
        if user:
            user.set_password(password)
            db.session.commit()
            log_audit_event(user_id=user.id, action='PASSWORD_RESET')
            flash('Your password has been updated! You can now login.', 'success')
            return redirect(url_for('auth.login'))
            
    return render_template('reset_password.html', token=token)
