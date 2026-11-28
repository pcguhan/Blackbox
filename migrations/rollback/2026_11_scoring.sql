SET @scoring_ddl = IF(
    EXISTS (SELECT 1 FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'solves'
              AND column_name = 'base_points_override'),
    'ALTER TABLE solves DROP COLUMN base_points_override',
    'SELECT 1'
);
PREPARE scoring_migration FROM @scoring_ddl;
EXECUTE scoring_migration;
DEALLOCATE PREPARE scoring_migration;
