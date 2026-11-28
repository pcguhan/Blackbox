-- Add optional profile settings without changing existing users or teams.
SET @backups_ddl = IF(
    EXISTS (SELECT 1 FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'nickname'),
    'SELECT 1', 'ALTER TABLE users ADD COLUMN nickname VARCHAR(80) NULL');
PREPARE backups_stmt FROM @backups_ddl;
EXECUTE backups_stmt;
DEALLOCATE PREPARE backups_stmt;

SET @backups_ddl = IF(
    EXISTS (SELECT 1 FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'avatar_filename'),
    'SELECT 1', 'ALTER TABLE users ADD COLUMN avatar_filename VARCHAR(64) NULL');
PREPARE backups_stmt FROM @backups_ddl;
EXECUTE backups_stmt;
DEALLOCATE PREPARE backups_stmt;
