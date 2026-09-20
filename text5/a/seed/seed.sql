-- Sample data for local development. Loaded automatically by docker-compose
-- (mounted into /docker-entrypoint-initdb.d). Idempotent via ON CONFLICT.

INSERT INTO departments (id, name, exempt) VALUES
    (1, 'Engineering', FALSE),
    (2, 'Sales', TRUE)          -- exempt: Sales activity is never stored
ON CONFLICT (id) DO NOTHING;
SELECT setval('departments_id_seq', 100);

INSERT INTO employees (id, department_id, name, timezone, role) VALUES
    (1, 1, 'Admin',        'UTC',           'admin'),
    (2, 1, 'Erin Manager', 'UTC',           'manager'),
    (3, 1, 'Alice',        'Asia/Shanghai', 'employee'),
    (4, 1, 'Bob',          'UTC',           'employee'),
    (5, 2, 'Sally Manager','UTC',           'manager'),
    (6, 2, 'Eve',          'UTC',           'employee')
ON CONFLICT (id) DO NOTHING;
SELECT setval('employees_id_seq', 100);

INSERT INTO workstations (id, employee_id) VALUES
    ('ws-alice', 3),
    ('ws-bob',   4),
    ('ws-eve',   6)
ON CONFLICT (id) DO NOTHING;

INSERT INTO api_tokens (token, employee_id) VALUES
    ('token-admin',     1),
    ('token-mgr-eng',   2),
    ('token-alice',     3),
    ('token-bob',       4),
    ('token-mgr-sales', 5)
ON CONFLICT (token) DO NOTHING;
