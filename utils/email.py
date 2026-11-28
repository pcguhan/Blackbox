import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import current_app
from itsdangerous import URLSafeTimedSerializer

import hashlib
import hmac

# Distinct salts: a verification token can never be replayed as a reset token
# (or vice versa), because each purpose signs with its own derived key.
EMAIL_CONFIRM_SALT = 'email-confirm-salt'
PASSWORD_RESET_SALT = 'password-reset-salt'
PASSWORD_RESET_MAX_AGE = 3600


def get_serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'])

def generate_confirmation_token(email):
    serializer = get_serializer()
    return serializer.dumps(email, salt=EMAIL_CONFIRM_SALT)

def verify_token(token, salt, expiration=3600):
    serializer = get_serializer()
    try:
        email = serializer.loads(token, salt=salt, max_age=expiration)
        return email
    except Exception:
        return False


def _password_fingerprint(user):
    """Short digest of the current password hash.

    Embedded in reset tokens so a token stops working as soon as the password
    changes (including through that same token), making it single-use.
    """
    return hashlib.sha256((user.password_hash or '').encode()).hexdigest()[:16]


def generate_password_reset_token(user):
    return get_serializer().dumps({'email': user.email, 'pw': _password_fingerprint(user)},
                                  salt=PASSWORD_RESET_SALT)


def verify_password_reset_token(token, expiration=PASSWORD_RESET_MAX_AGE):
    """Return the User for a valid, unused reset token, else None."""
    from models.user import User
    try:
        data = get_serializer().loads(token, salt=PASSWORD_RESET_SALT, max_age=expiration)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get('email'):
        return None
    user = User.query.filter_by(email=data['email']).first()
    if not user or not hmac.compare_digest(str(data.get('pw', '')), _password_fingerprint(user)):
        return None
    return user


def get_mail_settings():
    """SMTP settings: admin-set database values first, then MAIL_* environment config."""
    from models.settings import Settings
    cfg = current_app.config
    return {
        'username': Settings.get('mail_username') or cfg.get('MAIL_USERNAME') or '',
        'password': Settings.get('mail_password') or cfg.get('MAIL_PASSWORD') or '',
        'server': Settings.get('mail_server') or cfg.get('MAIL_SERVER') or 'smtp.gmail.com',
        'port': int(Settings.get('mail_port') or cfg.get('MAIL_PORT') or 587),
    }


def email_configured():
    mail = get_mail_settings()
    return bool(mail['username'] and mail['password'])

def send_email(to_email, subject, html_content):
    import logging
    logger = logging.getLogger(__name__)
    
    mail = get_mail_settings()
    sender_email = mail['username']
    sender_password = mail['password']
    smtp_server = mail['server']
    smtp_port = mail['port']
    
    logger.info(f"[EMAIL] Attempting to send '{subject}' to {to_email}")
    logger.info(f"[EMAIL] SMTP server: {smtp_server}:{smtp_port}")
    logger.info(f"[EMAIL] Sender configured: {bool(sender_email)} | Password configured: {bool(sender_password)}")
    
    if not sender_email or not sender_password:
        logger.error("[EMAIL] FAILED: SMTP credentials not configured (admin email settings or MAIL_USERNAME/MAIL_PASSWORD)")
        return False
        
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = to_email
    
    part = MIMEText(html_content, 'html')
    msg.attach(part)
    
    try:
        logger.info(f"[EMAIL] Connecting to {smtp_server}:{smtp_port} ...")
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
        logger.info("[EMAIL] Connected. Starting TLS...")
        server.starttls()
        logger.info("[EMAIL] TLS started. Logging in...")
        server.login(sender_email, sender_password)
        logger.info("[EMAIL] Logged in. Sending message...")
        server.sendmail(sender_email, to_email, msg.as_string())
        server.quit()
        logger.info(f"[EMAIL] SUCCESS: Email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"[EMAIL] FAILED to send to {to_email}: {type(e).__name__}: {str(e)}")
        return False

def send_email_async(app, to_email, subject, html_content):
    """Send email asynchronously (gevent greenlet) to avoid blocking the web request"""
    import logging
    logger = logging.getLogger(__name__)
    try:
        with app.app_context():
            send_email(to_email, subject, html_content)
    except Exception as e:
        logger.error(f"[EMAIL ASYNC] Unhandled exception in greenlet: {type(e).__name__}: {str(e)}")

