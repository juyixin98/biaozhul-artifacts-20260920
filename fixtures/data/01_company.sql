-- Synthetic local fixture for the documented acceptance walkthrough.
-- Reserved optional columns:
--   __row_id TEXT : explicit witness id (unique within the table)
--   __weight REAL : numeric weight injected to verify symbolic provenance
-- Native SQL NULL is used deliberately to demonstrate the NULL rule.

CREATE TABLE emp (
    eid      TEXT,
    dept     TEXT,
    __row_id TEXT,
    __weight REAL
);

INSERT INTO emp VALUES
    ('alice', 'Eng',   'e1', 2.0),
    ('alice', 'Eng',   'e6', 9.0),   -- value-duplicate of e1: distinct witness
    ('bob',   'Eng',   'e2', 3.0),
    ('carol', 'Sales', 'e3', 5.0),
    ('dave',  NULL,    'e4', 7.0);   -- NULL department

CREATE TABLE dept (
    dept     TEXT,
    budget   INTEGER,
    __row_id TEXT,
    __weight REAL
);

INSERT INTO dept VALUES
    ('Eng',   100, 'd1', 11.0),
    ('Sales', 200, 'd2', 13.0);

-- A second source of employee ids, so UNION can add duplicate derivations.
CREATE TABLE lead (
    eid      TEXT,
    dept     TEXT,
    __row_id TEXT,
    __weight REAL
);

INSERT INTO lead VALUES
    ('alice', 'Eng', 'l1', 17.0);
