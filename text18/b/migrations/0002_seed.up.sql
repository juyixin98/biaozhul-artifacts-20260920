-- Demo users (no external auth: identity is supplied via X-User-Id).
INSERT INTO users (id, name, role) VALUES
    ('u-analyst',   'Alice Analyst',   'analyst'),
    ('u-responder', 'Riley Responder', 'responder'),
    ('u-admin',     'Avery Admin',     'admin')
ON CONFLICT (id) DO NOTHING;
