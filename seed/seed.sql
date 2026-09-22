-- Idempotent demo seed. Executed by the migration runner once, on empty DBs.
-- API tokens are demo secrets, one per role.
INSERT INTO users (username, role, api_token) VALUES
    ('alice',  'member',    'token-alice'),
    ('bob',    'member',    'token-bob'),
    ('mod_tech',  'moderator', 'token-mod-tech'),
    ('mod_life',  'moderator', 'token-mod-life'),
    ('root',   'admin',     'token-admin')
ON CONFLICT (username) DO NOTHING;

INSERT INTO categories (name) VALUES
    ('tech'), ('life')
ON CONFLICT (name) DO NOTHING;

INSERT INTO moderator_categories (moderator_id, category_id)
SELECT u.id, c.id
FROM (VALUES ('mod_tech','tech'), ('mod_life','life')) AS v(mod_name, cat_name)
JOIN users u ON u.username = v.mod_name
JOIN categories c ON c.name = v.cat_name
ON CONFLICT DO NOTHING;

INSERT INTO rule_versions (version, note, active, activated_at)
SELECT 1, 'initial word list', true, now()
WHERE NOT EXISTS (SELECT 1 FROM rule_versions);

INSERT INTO sensitive_words (rule_version_id, word)
SELECT rv.id, w.word
FROM rule_versions rv
CROSS JOIN (VALUES ('spamword'), ('evilco'), ('forbidden-term')) AS w(word)
WHERE rv.version = 1
ON CONFLICT DO NOTHING;
