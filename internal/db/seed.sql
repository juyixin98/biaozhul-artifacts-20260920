-- Reference data and starter credentials. Idempotent: safe to run after every
-- migration; never mutates a database that already has published policy data.
--
-- Department 4 (Customer Success) is exempt from monitoring in the v1 policy.

insert into departments (id, name) values
    (1, 'Engineering'),
    (2, 'Sales'),
    (3, 'Finance'),
    (4, 'Customer Success')
on conflict (id) do nothing;

insert into employees (id, department_id, display_name, timezone) values
    (101, 1, 'Alice Wang',    'America/New_York'),
    (102, 1, 'Bob Chen',      'Asia/Shanghai'),
    (103, 2, 'Carol Diaz',    'Europe/Berlin'),
    (104, 4, 'Dan Murphy',    'America/New_York')
on conflict (id) do nothing;

insert into workstations (id, employee_id) values
    ('WS-101', 101),
    ('WS-102', 102),
    ('WS-103', 103),
    ('WS-104', 104)
on conflict (id) do nothing;

-- Sample keys. Replace in production via SQL or the admin endpoints.
insert into ingest_api_keys (key, workstation_id) values
    ('ingest-ws101', 'WS-101'),
    ('ingest-ws102', 'WS-102'),
    ('ingest-ws103', 'WS-103'),
    ('ingest-ws104', 'WS-104')
on conflict (key) do nothing;

insert into manager_api_keys (key, username, department_id) values
    ('mgr-eng', 'alice-mgr', 1),
    ('mgr-sales', 'carol-lead', 2),
    ('mgr-finance', 'fin-head', 3)
on conflict (key) do nothing;

-- Initial policy v1: window 08:00-18:00 local (half-open), private/secret
-- managers excluded, Customer Success (4) exempt.
insert into policy_versions
        (monitoring_start_minute, monitoring_end_minute, published_by, note)
select 480, 1080, 'seed', 'initial policy'
where not exists (select 1 from policy_versions);

insert into policy_excluded_apps (policy_version, position, pattern)
select version, pos, pat
from policy_versions
cross join (values
    (0, '1password*'),
    (1, 'keepass*'),
    (2, '*secret-manager*'),
    (3, '*vault*')
) as seed(pos, pat)
where version = 1
  and not exists (select 1 from policy_excluded_apps where policy_version = 1);

insert into policy_exempt_departments (policy_version, department_id)
select version, dept_id
from policy_versions, (values (4)) as seed(dept_id)
where version = 1
  and not exists (select 1 from policy_exempt_departments where policy_version = 1);

-- Initial classification v1.
insert into classification_versions (published_by, note)
select 'seed', 'initial classification'
where not exists (select 1 from classification_versions);

insert into classification_rules
        (classification_version, rule_id, pattern, category, priority)
select version, rid, pat, cat, pri
from classification_versions
cross join (values
    ('code-01',       'code',           'productive',   10),
    ('code-02',       'goland*',        'productive',   10),
    ('code-03',       'vs code*',       'productive',   10),
    ('browser-01',    'chrome*',        'productive',   20),
    ('meet-01',       'zoom*',          'productive',   15),
    ('game-01',       '*game*',         'unproductive', 30),
    ('social-01',     'wechat*',        'unproductive', 30),
    ('social-02',     'tiktok*',        'unproductive', 30)
) as seed(rid, pat, cat, pri)
where version = 1
  and not exists (select 1 from classification_rules where classification_version = 1);
