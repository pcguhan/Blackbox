import gevent.monkey
gevent.monkey.patch_all()

from flask import Flask, render_template, send_from_directory, send_file, abort, flash, redirect, request, url_for
from flask_login import LoginManager, current_user, login_required
from flask.json.provider import DefaultJSONProvider
from config import config
from models import db
from models.user import User
from scripts.schema_backups import ensure_profile_schema
from services.cache import cache_service
from services.websocket import WebSocketService, socketio
from services.file_storage import file_storage
from services.backup_paths import init_backup_paths
from routes.profile_settings import profile_settings_bp
from routes.admin_versions import admin_versions_bp
from security_utils import CSRFProtection, init_security
import os
from decimal import Decimal

class DecimalJSONProvider(DefaultJSONProvider):
    """Custom JSON provider to handle Decimal objects"""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)

def create_app(config_name=None):
    """Create and configure the Flask application"""
    
    if config_name is None:
        config_name = os.getenv('FLASK_ENV', 'development')
    
    # Explicitly set static folder path
    static_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    
    app = Flask(__name__, static_folder=static_folder, static_url_path='/static')
    app.config.from_object(config[config_name])
    init_backup_paths(app)
    
    # Set custom JSON provider
    app.json = DecimalJSONProvider(app)
    
    # Initialize extensions
    db.init_app(app)
    cache_service.init_app(app)
    WebSocketService.init_app(app)
    file_storage.init_app(app)
    
    # Initialize security features (CSRF, security headers, etc.)
    init_security(app)
    
    # Add ProxyFix to prevent IP spoofing.
    # Trust counts are read from env vars so each deployment can opt-in explicitly.
    # Default is 0 (disabled) — a direct gunicorn deployment with no reverse proxy
    # must NOT trust X-Forwarded-For headers, otherwise clients can spoof their IP.
    # Set PROXY_FIX_X_FOR=1 (and the others) in your environment / docker-compose
    # when running behind exactly one trusted Nginx/HAProxy reverse proxy.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=int(os.getenv('PROXY_FIX_X_FOR', 0)),
        x_proto=int(os.getenv('PROXY_FIX_X_PROTO', 0)),
        x_host=int(os.getenv('PROXY_FIX_X_HOST', 0)),
        x_prefix=int(os.getenv('PROXY_FIX_X_PREFIX', 0)),
    )
    
    # Initialize Flask-Login
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = None
    
    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    @app.before_request
    @CSRFProtection.csrf_protect()
    def protect_admin_routes():
        if request.path == '/admin' or request.path.startswith('/admin/'):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if not current_user.is_admin:
                if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                    abort(403)
                flash('Admin access required', 'error')
                return redirect(url_for('index'))
    
    # Register blueprints
    from routes.auth import auth_bp
    from routes.auth_admin import auth_admin_bp
    from routes.auth_google import auth_google_bp
    from routes.challenges import challenges_bp
    from routes.teams import teams_bp
    from routes.scoreboard import scoreboard_bp
    from routes.admin import admin_bp
    from routes.access_admin import access_admin_bp
    from routes.notifications import notifications_bp
    from routes.setup import setup_bp
    from routes.hints import hints_bp
    from routes.container import container_bp
    from routes.appearance_admin import appearance_admin_bp
    from routes.docker_upload import docker_upload_bp
    from routes.guide import guide_bp
    
    app.register_blueprint(setup_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(auth_admin_bp)
    app.register_blueprint(auth_google_bp)
    app.register_blueprint(challenges_bp)
    app.register_blueprint(teams_bp)
    app.register_blueprint(scoreboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(access_admin_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(hints_bp)
    app.register_blueprint(container_bp)

    @app.before_request
    def recheck_session_access():
        from flask import flash, g, jsonify, redirect, request, url_for
        from flask_login import current_user, logout_user
        from services.exclusive_access import BLOCKED_MESSAGE, DEACTIVATED_MESSAGE, enforce_for_user

        if current_user.is_anonymous or enforce_for_user(current_user):
            return None

        message = BLOCKED_MESSAGE if current_user.is_active else DEACTIVATED_MESSAGE
        g.access_revoked = True
        logout_user()
        if '/api/' in request.path or request.accept_mimetypes.best == 'application/json':
            return jsonify(success=False, message=message), 403
        flash(message, 'error')
        return redirect(url_for('auth.login'))

    @app.after_request
    def prevent_caching_signed_in_responses(response):
        from flask import g
        from flask_login import current_user

        if response.mimetype == 'text/html' or response.is_json:
            if current_user.is_authenticated or g.get('access_revoked'):
                response.headers['Cache-Control'] = 'no-store'
        return response
    app.register_blueprint(appearance_admin_bp)
    app.register_blueprint(docker_upload_bp)
    app.register_blueprint(profile_settings_bp)
    app.register_blueprint(admin_versions_bp)
    app.register_blueprint(guide_bp)
    
    # Setup check middleware
    @app.before_request
    def check_setup():
        """Redirect to setup if no admin exists"""
        from flask import request
        from routes.setup import is_setup_complete
        
        if request.path.startswith('/setup') or \
           request.path.startswith('/static') or \
           request.path.startswith('/health') or \
           request.path.startswith('/files'):
            return None
        
        try:
            if not is_setup_complete():
                from flask import redirect, url_for
                return redirect(url_for('setup.initial_setup'))
        except:
            pass
        
        return None
    
    # Main routes
    @app.route('/')
    def index():
        """Homepage"""
        return render_template('index.html')

    @app.route('/api/event-status')
    def event_status():
        from models.settings import Settings
        return app.json.response(started=Settings.is_ctf_started())

    @app.route('/about')
    def about():
        """About page"""
        from models.settings import Settings
        act_system_enabled = Settings.get('act_system_enabled', default=False, type='bool')
        return render_template('about.html', act_system_enabled=act_system_enabled)
    
    @app.route('/uploads/<path:filename>')
    def serve_logo(filename):
        """Serve uploaded logo files from /var/uploads/logos"""
        logos_folder = app.config['LOGO_UPLOAD_FOLDER']
        try:
            response = send_from_directory(logos_folder, filename)
            response.headers['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; sandbox"
            return response
        except FileNotFoundError:
            abort(404)
    
    @app.route('/files/<path:filename>')
    @login_required
    def serve_file(filename):
        """Serve uploaded files with original filename"""
        from models.file import ChallengeFile
        from services.challenge_catalog import is_challenge_visible

        if '\\' in filename or ':' in filename or any(part in ('', '.', '..') for part in filename.split('/')):
            abort(404)

        file_record = ChallengeFile.query.filter(
            ChallengeFile.relative_path.in_((filename, filename.replace('/', '\\')))
        ).first()
        if not file_record or not is_challenge_visible(file_record.challenge, current_user):
            abort(404)

        upload_folder = os.path.realpath(app.config.get('UPLOAD_FOLDER', 'uploads'))
        stored_path = file_record.relative_path.replace('\\', '/')
        file_path = os.path.realpath(os.path.join(upload_folder, stored_path))
        legacy_backups = os.path.realpath(os.path.join(upload_folder, 'backups'))
        if (not file_path.startswith(upload_folder + os.sep) or not os.path.isfile(file_path)
                or file_path.startswith(legacy_backups + os.sep) or stored_path.split('/', 1)[0] == 'backups'):
            abort(404)

        if (file_record.original_filename or '').rstrip(' .').casefold() == 'solution.md':
            abort(404)

        response = send_file(
            file_path,
            as_attachment=True,
            download_name=file_record.original_filename,
            mimetype=file_record.mime_type or 'application/octet-stream'
        )
        response.cache_control.private = True
        response.cache_control.no_store = True
        return response
    
    @app.route('/favicon.ico')
    def favicon():
        """Serve favicon"""
        return send_from_directory(
            os.path.join(app.root_path, 'static'),
            'favicon.ico',
            mimetype='image/vnd.microsoft.icon'
        )
    
    @app.errorhandler(404)
    def not_found(error):
        return render_template('errors/404.html'), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return render_template('errors/500.html'), 500
    
    @app.route('/health')
    def health_check():
        """Health check endpoint for load balancers and monitoring"""
        from datetime import datetime
        try:
            # Check database connectivity
            db.session.execute(db.text('SELECT 1'))
            db_status = 'healthy'
        except Exception as e:
            db_status = f'unhealthy: {str(e)}'
        
        try:
            # Check Redis connectivity
            cache_service.redis_client.ping()
            redis_status = 'healthy'
        except Exception as e:
            redis_status = f'unhealthy: {str(e)}'
        
        # Overall health
        is_healthy = db_status == 'healthy' and redis_status == 'healthy'
        
        health_data = {
            'status': 'healthy' if is_healthy else 'unhealthy',
            'timestamp': datetime.utcnow().isoformat(),
            'checks': {
                'database': db_status,
                'redis': redis_status
            },
            'config': {
                'workers': os.getenv('WORKERS', '1'),
                'worker_class': os.getenv('WORKER_CLASS', 'eventlet')
            }
        }
        
        status_code = 200 if is_healthy else 503
        return app.json.response(**health_data), status_code
    
    @app.context_processor
    def inject_config():
        """Inject configuration into all templates"""
        from models.settings import Settings
        
        # Load from database settings (with fallback to config)
        ctf_name = Settings.get('ctf_name', app.config.get('CTF_NAME', 'CYSCOM'))
        ctf_description = Settings.get('ctf_description', app.config.get('CTF_DESCRIPTION', ''))
        allow_registration = Settings.get('allow_registration', True)
        ctf_logo = Settings.get('ctf_logo', '')
        teams_enabled = Settings.get('teams_enabled', True, type='bool')
        scoreboard_visible = Settings.get('scoreboard_visible', True, type='bool')
        
        return {
            'ctf_name': ctf_name,
            'ctf_description': ctf_description,
            'registration_enabled': allow_registration,
            'ctf_logo': ctf_logo,
            'teams_enabled': teams_enabled,
            'scoreboard_visible': scoreboard_visible,
            'competition_state': Settings.get_ctf_status(),
            'settings': Settings
        }
    
    @app.template_filter('format_datetime')
    def format_datetime_filter(dt, format_str='%Y-%m-%d %H:%M:%S'):
        """Template filter to format datetime in platform timezone"""
        from utils.timezone import format_datetime
        return format_datetime(dt, format_str)
    
    @app.template_filter('to_platform_tz')
    def to_platform_tz_filter(dt):
        """Template filter to convert datetime to platform timezone"""
        from utils.timezone import convert_to_platform_tz
        return convert_to_platform_tz(dt)
    
    return app


def main():
    """Main entry point"""
    app = create_app()
    
    # Initialize backup scheduler after app is created
    with app.app_context():
        try:
            from services.backup_scheduler import init_backup_scheduler
            scheduler = init_backup_scheduler(app)
            # Trigger initial schedule setup
            scheduler.reschedule()
        except Exception as e:
            app.logger.warning(f"Could not initialize backup scheduler: {e}")
    
    socketio.run(
        app,
        host=app.config.get('HOST', '0.0.0.0'),
        port=app.config.get('PORT', 5000),
        debug=app.config.get('DEBUG', True),
        use_reloader=True
    )


app = create_app()

if not app.testing:
    # Initialize backup scheduler for production (gunicorn)
    with app.app_context():
        try:
            from services.backup_scheduler import init_backup_scheduler
            scheduler = init_backup_scheduler(app)
            scheduler.reschedule()
        except Exception as e:
            app.logger.warning(f"Could not initialize backup scheduler during app creation: {e}")

        # Ensure DB schema has required docker-related columns and defaults
        try:
            from scripts.db_schema import ensure_docker_schema
            ensure_docker_schema()
            ensure_profile_schema()
            app.logger.info("Ensured docker DB schema and defaults")
        except Exception as e:
            app.logger.warning(f"Could not ensure docker DB schema on startup: {e}")

        from scripts.schema_scoring import ensure_scoring_schema
        ensure_scoring_schema()
        try:
            from scripts.schema_auth import ensure_auth_schema
            ensure_auth_schema()
        except Exception as e:
            app.logger.warning(f"Could not ensure authentication DB schema on startup: {e}")

    # Initialize container reconciliation background task
    try:
        import threading
        from services.container_reconciliation import run_reconciliation_loop

        reconciliation_thread = threading.Thread(
            target=run_reconciliation_loop,
            args=(app, 60),  # Check every 60 seconds
            daemon=True,
            name="ContainerReconciliation"
        )
        reconciliation_thread.start()
        app.logger.info("Container reconciliation background task started")
    except Exception as e:
        app.logger.warning(f"Could not start container reconciliation task: {e}")


if __name__ == '__main__':
    main()
