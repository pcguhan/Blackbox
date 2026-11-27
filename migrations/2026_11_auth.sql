-- Add Google identities without changing existing accounts or team memberships.
SET @auth_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'google_sub'
), 'SELECT 1', 'ALTER TABLE users ADD COLUMN google_sub VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NULL');
PREPARE auth_stmt FROM @auth_ddl;
EXECUTE auth_stmt;
DEALLOCATE PREPARE auth_stmt;

SET @auth_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'roll_number'
), 'SELECT 1', 'ALTER TABLE users ADD COLUMN roll_number VARCHAR(9) NULL');
PREPARE auth_stmt FROM @auth_ddl;
EXECUTE auth_stmt;
DEALLOCATE PREPARE auth_stmt;

SET @auth_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'auth_provider'
), 'SELECT 1', 'ALTER TABLE users ADD COLUMN auth_provider VARCHAR(6) NOT NULL DEFAULT ''local''');
PREPARE auth_stmt FROM @auth_ddl;
EXECUTE auth_stmt;
DEALLOCATE PREPARE auth_stmt;

SET @auth_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND INDEX_NAME = 'ix_users_google_sub'
), 'SELECT 1', 'CREATE UNIQUE INDEX ix_users_google_sub ON users (google_sub)');
PREPARE auth_stmt FROM @auth_ddl;
EXECUTE auth_stmt;
DEALLOCATE PREPARE auth_stmt;
