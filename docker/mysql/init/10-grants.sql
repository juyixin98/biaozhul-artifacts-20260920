-- Runs only on first database initialization (empty data volume).
-- The app needs to create/destroy the Django test database in development
-- and CI; grants are scoped to the app + test schemas, never *.*.
GRANT ALL PRIVILEGES ON `fieldsnap`.* TO 'fieldsnap'@'%';
GRANT ALL PRIVILEGES ON `test_fieldsnap`.* TO 'fieldsnap'@'%';
FLUSH PRIVILEGES;
