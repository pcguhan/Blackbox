-- Remove only the optional fields added by the backups migration.
SET @backups_ddl = IF(
    EXISTS (SELECT 1 FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'avatar_filename'),
    'ALTER TABLE users DROP COLUMN avatar_filename', 'SELECT 1');
PREPARE backups_stmt FROM @backups_ddl;
EXECUTE backups_stmt;
DEALLOCATE PREPARE backups_stmt;

SET @backups_ddl = IF(
    EXISTS (SELECT 1 FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'nickname'),
    'ALTER TABLE users DROP COLUMN nickname', 'SELECT 1');
PREPARE backups_stmt FROM @backups_ddl;
EXECUTE backups_stmt;
DEALLOCATE PREPARE backups_stmt;
