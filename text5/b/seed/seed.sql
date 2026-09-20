-- Demo seed data (idempotent). Applied only when SEED_DEMO_DATA=true.

INSERT INTO departments (id, name) VALUES
    (1, 'Engineering'),
    (2, 'Sales'),
    (3, 'Executive')          -- exempt from monitoring
ON CONFLICT (id) DO NOTHING;
SELECT setval('departments_id_seq', (SELECT MAX(id) FROM departments));

INSERT INTO employees (id, department_id, name, timezone) VALUES
    (1, 1, 'Alice', 'Asia/Shanghai'),
    (2, 1, 'Bob',   'Europe/Berlin'),
    (3, 2, 'Carol', 'America/New_York'),
    (4, 3, 'Dave',  'UTC')
ON CONFLICT (id) DO NOTHING;
SELECT setval('employees_id_seq', (SELECT MAX(id) FROM employees));

-- Tokens are demo-only; real deployments should store hashes.
INSERT INTO users (username, token, role, department_id) VALUES
    ('admin',       'admin-token',   'admin',   NULL),
    ('ingest-svc',  'ingest-token',  'ingest',  NULL),
    ('mgr-eng',     'mgr-eng-token', 'manager', 1),
    ('mgr-sales',   'mgr-sales-token','manager', 2)
ON CONFLICT (username) DO NOTHING;

-- Policy v1: monitor 09:00-18:00 local, Mon-Fri; exclude a couple of apps;
-- Executive department is exempt.
INSERT INTO policy_versions (work_start_minutes, work_end_minutes, workdays, excluded_apps, exempt_department_ids)
SELECT 9*60, 18*60, ARRAY[1,2,3,4,5]::smallint[],
       ARRAY['1Password', 'Signal'],
       ARRAY[3]::int[]
WHERE NOT EXISTS (SELECT 1 FROM policy_versions);

-- Classification v1.
DO $$
DECLARE v int;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM classification_versions) THEN
        INSERT INTO classification_versions DEFAULT VALUES RETURNING version INTO v;
        INSERT INTO classification_rules (version, pattern, category, priority) VALUES
            (v, '*Code*',        'productive',   10),
            (v, '*Terminal*',    'productive',   10),
            (v, '*Slack*',       'neutral',       5),
            (v, '*YouTube*',     'unproductive', 10),
            (v, '*Netflix*',     'unproductive', 10),
            (v, '*',             'neutral',       0);
    END IF;
END $$;
