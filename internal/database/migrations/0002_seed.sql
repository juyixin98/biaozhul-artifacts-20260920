-- Seed: two monitored departments plus an exempt one, sample staff and
-- workstations, published policy/classification versions and API tokens.

INSERT INTO departments (id, name, exempt) VALUES
    (1, 'Engineering', FALSE),
    (2, 'Sales',       FALSE),
    (3, 'Legal',       TRUE);

INSERT INTO employees (id, department_id, full_name, timezone, active) VALUES
    (101, 1, 'Ada Lovelace',  'Europe/London', TRUE),
    (102, 1, 'Alan Turing',   'America/New_York', TRUE),
    (103, 2, 'Grace Hopper',  'America/Los_Angeles', TRUE),
    (104, 3, 'Counsellor Kim','Asia/Tokyo', TRUE);

INSERT INTO workstations (id, employee_id, label) VALUES
    (1001, 101, 'ada-desk'),
    (1002, 102, 'alan-desk'),
    (1003, 103, 'grace-desk'),
    (1004, 104, 'kim-desk');

INSERT INTO policy_versions (version, window_start_minute, window_end_minute, published_at) VALUES
    (1, 9 * 60, 18 * 60, '2026-01-01T00:00:00Z');

-- Privacy/security tooling is dropped before it ever reaches raw storage.
INSERT INTO policy_excluded_apps (policy_version, pattern) VALUES
    (1, '1password*'),
    (1, 'bitwarden*'),
    (1, '*keychain*');

INSERT INTO policy_exempt_departments (policy_version, department_id) VALUES
    (1, 3);

INSERT INTO classification_versions (version, published_at) VALUES
    (1, '2026-01-01T00:00:00Z');

-- Priority: higher number wins; ties resolve to the lower rule_id.
INSERT INTO classification_rules (version, rule_id, pattern, category, priority) VALUES
    (1, 1, 'vs code',           'productive',     100),
    (1, 2, 'intellij*',         'productive',     100),
    (1, 3, 'github desktop',    'productive',     100),
    (1, 4, '*terminal*',        'productive',     90),
    (1, 5, 'slack',             'non_productive', 50),
    (1, 6, 'zoom*',             'non_productive', 50),
    (1, 7, 'firefox*',          'neutral',        10),
    (1, 8, '*',                 'neutral',        0);

-- Demo tokens. Replace in production.
INSERT INTO api_tokens (token, role, department_id, description) VALUES
    ('admin-token',         'admin',   NULL, 'demo admin'),
    ('manager-eng-token',   'manager', 1,    'demo engineering manager'),
    ('manager-sales-token', 'manager', 2,    'demo sales manager');
