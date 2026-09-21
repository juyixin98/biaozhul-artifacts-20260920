-- Seed demo data. Idempotent: safe to run on every fresh database.

INSERT INTO users (id, username, role) VALUES
    (1, 'alice',  'member'),
    (2, 'bob',    'member'),
    (3, 'mod_tech', 'moderator'),
    (4, 'mod_art',  'moderator'),
    (5, 'admin',    'admin')
ON CONFLICT (id) DO NOTHING;
SELECT setval(pg_get_serial_sequence('users', 'id'), (SELECT max(id) FROM users));

INSERT INTO moderator_scopes (user_id, category) VALUES
    (3, 'tech'),
    (4, 'art')
ON CONFLICT DO NOTHING;

-- Baseline active rule version with one word.
INSERT INTO rule_versions (id, version, status, description, created_by)
VALUES (1, 1, 'active', 'baseline word list', 5)
ON CONFLICT (id) DO NOTHING;
SELECT setval(pg_get_serial_sequence('rule_versions', 'id'), (SELECT max(id) FROM rule_versions));

INSERT INTO rule_words (rule_version_id, word) VALUES
    (1, 'forbidden')
ON CONFLICT DO NOTHING;
