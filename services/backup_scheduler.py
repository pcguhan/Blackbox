"""
Automatic backup scheduler for the CTF platform.
Handles scheduled database backups based on configured frequency.
"""

import gzip
import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pymysql
from apscheduler.schedulers.background import BackgroundScheduler

from models.settings import Settings
from services.backup_paths import archive_uploads, backup_dir, delete_backup_files, publish_backup_file, regular_file

logger = logging.getLogger(__name__)


class BackupScheduler:
    """Manages automatic database backups"""
    
    def __init__(self, app=None):
        self.app = app
        self.scheduler = None
        
        if app is not None:
            self.init_app(app)
    
    def init_app(self, app):
        """Initialize the backup scheduler with Flask app"""
        self.app = app
        self.scheduler = BackgroundScheduler(daemon=True)
        
        # Start the scheduler (schedule will be set on first settings access)
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Backup scheduler started (will schedule on first settings access)")
    
    def _schedule_backups(self):
        """Configure backup schedule based on settings"""
        try:
            frequency = Settings.get('backup_frequency', 'disabled')
        except Exception as e:
            # Database might not be ready yet, use default
            logger.warning(f"Could not read backup frequency setting: {e}")
            frequency = 'disabled'
        
        # Remove existing backup job if any
        if self.scheduler.get_job('auto_backup'):
            self.scheduler.remove_job('auto_backup')
        
        if frequency == 'disabled':
            logger.info("Automatic backups are disabled")
            return
        
        # Run in the event's configured timezone, not the server's.
        try:
            from utils.timezone import get_platform_timezone
            tz = get_platform_timezone()
        except Exception as e:
            logger.warning(f"Could not read platform timezone, using UTC: {e}")
            import pytz
            tz = pytz.UTC

        # Schedule based on frequency
        if frequency == 'hourly':
            self.scheduler.add_job(
                self.create_automatic_backup,
                'cron',
                minute=0,  # Every hour at :00
                id='auto_backup',
                timezone=tz,
                replace_existing=True
            )
            logger.info("Automatic backups scheduled: Every hour at :00")
        
        elif frequency == 'daily':
            self.scheduler.add_job(
                self.create_automatic_backup,
                'cron',
                hour=2,
                minute=0,  # Daily at 2:00 AM
                id='auto_backup',
                timezone=tz,
                replace_existing=True
            )
            logger.info("Automatic backups scheduled: Daily at 2:00 AM")
        
        elif frequency == 'weekly':
            self.scheduler.add_job(
                self.create_automatic_backup,
                'cron',
                day_of_week='sun',
                hour=2,
                minute=0,  # Sunday at 2:00 AM
                id='auto_backup',
                timezone=tz,
                replace_existing=True
            )
            logger.info("Automatic backups scheduled: Weekly on Sunday at 2:00 AM")
        
        elif frequency == 'monthly':
            self.scheduler.add_job(
                self.create_automatic_backup,
                'cron',
                day=1,
                hour=2,
                minute=0,  # 1st day of month at 2:00 AM
                id='auto_backup',
                timezone=tz,
                replace_existing=True
            )
            logger.info("Automatic backups scheduled: Monthly on 1st at 2:00 AM")
    
    def reschedule(self):
        """Reschedule backups (call after settings change)"""
        logger.info("Rescheduling automatic backups")
        self._schedule_backups()
    
    def create_automatic_backup(self):
        """Create an automatic backup"""
        logger.info("Starting automatic backup...")
        
        try:
            with self.app.app_context():
                # Get database connection info
                db_uri = self.app.config.get('SQLALCHEMY_DATABASE_URI')
                
                # Create backup directory
                directory = backup_dir()
                
                # Generate backup name
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                backup_name = f'backup_auto_{timestamp}'
                backup_file = directory / f'{backup_name}.sql.gz'
                
                # Export database to SQL dump
                parsed = urlparse(db_uri)
                conn = pymysql.connect(
                    host=parsed.hostname,
                    port=parsed.port or 3306,
                    user=parsed.username,
                    password=parsed.password,
                    database=parsed.path.lstrip('/')
                )
                
                cursor = conn.cursor()
                
                # Get all tables
                cursor.execute("SHOW TABLES")
                tables = [table[0] for table in cursor.fetchall()]
                
                # Create SQL dump
                sql_dump = []
                sql_dump.append(f"-- Automatic database backup: {backup_name}")
                sql_dump.append(f"-- Timestamp: {datetime.now().isoformat()}")
                sql_dump.append("SET FOREIGN_KEY_CHECKS=0;")
                
                for table in tables:
                    # Get CREATE TABLE statement
                    cursor.execute(f"SHOW CREATE TABLE `{table}`")
                    create_table = cursor.fetchone()[1]
                    sql_dump.append(f"\n-- Table: {table}")
                    sql_dump.append(f"DROP TABLE IF EXISTS `{table}`;")
                    sql_dump.append(create_table + ";")
                    
                    # Get table data
                    cursor.execute(f"SELECT * FROM `{table}`")
                    rows = cursor.fetchall()
                    
                    if rows:
                        from services.backup_sql import insert_statement
                        cursor.execute(f"DESCRIBE `{table}`")
                        columns = [col[0] for col in cursor.fetchall()]
                        for row in rows:
                            sql_dump.append(insert_statement(table, columns, row))

                sql_dump.append("\nSET FOREIGN_KEY_CHECKS=1;")
                
                cursor.close()
                conn.close()
                
                # Write compressed backup
                with publish_backup_file(backup_file) as staged, gzip.open(staged, 'wt', encoding='utf-8') as f:
                    f.write('\n'.join(sql_dump))
                # Optional: include uploads and redis snapshot (best-effort)
                components = {
                    'database': True,
                    'uploads': False,
                    'redis': False
                }

                sizes = {
                    'database_mb': round(backup_file.stat().st_size / (1024 * 1024), 2),
                    'uploads_mb': 0,
                    'redis_mb': 0
                }

                # Include uploads directory if enabled in settings
                try:
                    include_uploads = Settings.get('backup_include_uploads', False)
                except Exception:
                    include_uploads = False

                if include_uploads:
                    try:
                        uploads_dir = Path(self.app.config.get('UPLOAD_FOLDER', 'static/uploads'))
                        uploads_archive = directory / f"{backup_name}_uploads.tar.gz"
                        # Create tarball of uploads (skip if missing)
                        if uploads_dir.exists():
                            archive_uploads(uploads_dir, uploads_archive)
                            components['uploads'] = True
                            sizes['uploads_mb'] = round(uploads_archive.stat().st_size / (1024 * 1024), 2)
                    except Exception as e:
                        logger.warning(f"Failed to include uploads in backup: {e}")

                # Attempt to include Redis RDB snapshot (best-effort)
                try:
                    include_redis = Settings.get('backup_include_redis', False)
                except Exception:
                    include_redis = False

                if include_redis:
                    try:
                        # Best-effort: ask Redis to save and then copy the dump if accessible
                        import redis as redislib
                        redis_url = self.app.config.get('REDIS_URL')
                        if redis_url:
                            r = redislib.from_url(redis_url)
                            # Use BGSAVE to avoid blocking
                            try:
                                r.bgsave()
                            except Exception:
                                # Some managed Redis may not permit bgsave; try SAVE
                                try:
                                    r.save()
                                except Exception:
                                    pass

                            # Try to locate Redis dump path via CONFIG
                            try:
                                cfg = r.config_get('dir')
                                dirpath = cfg.get('dir') if isinstance(cfg, dict) else None
                                dbfile = r.config_get('dbfilename')
                                filename = dbfile.get('dbfilename') if isinstance(dbfile, dict) else None
                                if dirpath and filename:
                                    dump_path = Path(dirpath) / filename
                                    if dump_path.exists():
                                        target = directory / f"{backup_name}_redis.rdb"
                                        import shutil
                                        with publish_backup_file(target) as staged:
                                            shutil.copyfile(dump_path, staged)
                                        components['redis'] = True
                                        sizes['redis_mb'] = round(target.stat().st_size / (1024 * 1024), 2)
                            except Exception:
                                # If config_get or file access fails, skip redis
                                logger.debug('Could not copy redis dump file; skipping')
                    except Exception as e:
                        logger.warning(f"Failed to include redis in backup: {e}")

                # Create metadata file
                metadata = {
                    'backup_name': backup_name,
                    'timestamp': datetime.now().isoformat(),
                    'size_mb': sizes['database_mb'],
                    'auto_backup': True,
                    'tables': len(tables),
                    'components': components,
                    'sizes': sizes
                }

                metadata_file = directory / f'{backup_name}.json'
                with publish_backup_file(metadata_file) as staged, staged.open('w', encoding='utf-8') as f:
                    json.dump(metadata, f, indent=2)
                
                # Update last backup time
                Settings.set('last_auto_backup', datetime.now(), 'datetime', 'Last automatic backup')
                
                logger.info(f"Automatic backup created successfully: {backup_name}")
                logger.info(f"Backup size: {metadata['size_mb']} MB, Tables: {metadata['tables']}")
                
                # Keep only last 10 automatic backups
                self._cleanup_old_backups(directory)
        
        except Exception as e:
            logger.error(f"Automatic backup failed: {str(e)}", exc_info=True)
    
    def _cleanup_old_backups(self, directory):
        """Keep only the most recent automatic backups"""
        try:
            max_backups = 10  
            
            auto_backups = sorted(
                (path for path in directory.glob('backup_auto_*.sql.gz') if regular_file(path)),
                key=lambda f: f.stat().st_mtime,
                reverse=True
            )
            
            for backup_file in auto_backups[max_backups:]:
                delete_backup_files(directory, backup_file.name.removesuffix('.sql.gz'))
                
                logger.info(f"Removed old automatic backup: {backup_file.name}")
        
        except Exception as e:
            logger.error(f"Failed to cleanup old backups: {str(e)}")


backup_scheduler = None


def init_backup_scheduler(app):
    """Initialize the global backup scheduler"""
    global backup_scheduler
    backup_scheduler = BackupScheduler(app)
    return backup_scheduler
