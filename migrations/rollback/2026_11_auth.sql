-- Run with the auth-enabled application stopped. Account rows and teams are retained.
DELETE FROM settings WHERE `key` IN ('auth_google_enabled', 'auth_google_only');

SET @auth_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'auth_provider'
), 'ALTER TABLE users DROP COLUMN auth_provider', 'SELECT 1');
PREPARE auth_stmt FROM @auth_ddl;
EXECUTE auth_stmt;
DEALLOCATE PREPARE auth_stmt;

SET @auth_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'roll_number'
), 'ALTER TABLE users DROP COLUMN roll_number', 'SELECT 1');
PREPARE auth_stmt FROM @auth_ddl;
EXECUTE auth_stmt;
DEALLOCATE PREPARE auth_stmt;

-- Dropping google_sub also removes its single-column index.
SET @auth_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'google_sub'
), 'ALTER TABLE users DROP COLUMN google_sub', 'SELECT 1');
PREPARE auth_stmt FROM @auth_ddl;
EXECUTE auth_stmt;
DEALLOCATE PREPARE auth_stmt;
