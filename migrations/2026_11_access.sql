CREATE TABLE IF NOT EXISTS access_allowlist (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(120) NOT NULL,
    added_at DATETIME NOT NULL,
    UNIQUE KEY ix_access_allowlist_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
