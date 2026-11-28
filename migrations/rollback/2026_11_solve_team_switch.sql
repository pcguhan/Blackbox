-- Restore UNIQUE(user_id, challenge_id). Fails if a user has solves for two teams;
-- remove the extra rows first.
SET @solve_ddl = IF(
    EXISTS (SELECT 1 FROM information_schema.statistics
            WHERE table_schema = DATABASE() AND table_name = 'solves'
              AND index_name = 'unique_user_challenge'),
    'SELECT 1',
    'CREATE UNIQUE INDEX unique_user_challenge ON solves (user_id, challenge_id)'
);
PREPARE solve_migration FROM @solve_ddl;
EXECUTE solve_migration;
DEALLOCATE PREPARE solve_migration;
SET @solve_ddl = IF(
    EXISTS (SELECT 1 FROM information_schema.statistics
            WHERE table_schema = DATABASE() AND table_name = 'solves'
              AND index_name = 'unique_user_team_challenge'),
    'ALTER TABLE solves DROP INDEX unique_user_team_challenge',
    'SELECT 1'
);
PREPARE solve_migration FROM @solve_ddl;
EXECUTE solve_migration;
DEALLOCATE PREPARE solve_migration;
