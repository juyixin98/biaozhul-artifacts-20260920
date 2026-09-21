-- Seed users (password for all three: Pass#2026) and sample data.
-- December 2025 is already closed; the seeded ledger for A001/A002/A004 is booked
-- through 2025-12 so the demo's first live action is the January 2026 month-end run.

INSERT INTO app_user (id, username, password_hash, role, enabled, created_at) VALUES
    (1, 'finance', '$2a$10$awQjaDzNTv0P0b4OK8UpC.GzZT.BcH087.rMFjy1.2qf0GCklJuOG', 'FINANCE',       TRUE, NOW(6)),
    (2, 'manager', '$2a$10$awQjaDzNTv0P0b4OK8UpC.GzZT.BcH087.rMFjy1.2qf0GCklJuOG', 'ASSET_MANAGER', TRUE, NOW(6)),
    (3, 'viewer',  '$2a$10$awQjaDzNTv0P0b4OK8UpC.GzZT.BcH087.rMFjy1.2qf0GCklJuOG', 'VIEWER',        TRUE, NOW(6));

INSERT INTO hardware_asset
(id, asset_code, name, department, status, cost, salvage_value, in_service_date, useful_life_months,
 used_months, method, nbv, sl_monthly, ddb_rate, exit_period, last_posted_period, version, created_at, updated_at)
VALUES
-- A001: straight line 12000-1200 over 36m = 300.00/m; five months booked (2025-08..12).
(1, 'A001', 'Dell Latitude 5450',    'IT',         'IN_USE',       12000.00, 1200.00, DATE '2025-07-20', 36, 5, 'SL',  10500.00, 300.00,     NULL,       NULL, '2025-12', 1, NOW(6), NOW(6)),
-- A002: double declining balance over 48m; 2025-11: 1000.00, 2025-12: 958.33.
(2, 'A002', 'MacBook Pro M4',        'R&D',        'IN_USE',       24000.00, 2400.00, DATE '2025-10-02', 48, 2, 'DDB', 22041.67, NULL,       0.04166667, NULL, '2025-12', 1, NOW(6), NOW(6)),
-- A003: never activated, no depreciation parameters yet.
(3, 'A003', 'HP LaserJet Pro',       'Admin',      'IN_STOCK',      5000.00,  500.00, NULL,              NULL, 0, 'SL',   5000.00, NULL,       NULL,       NULL, NULL,       0, NOW(6), NOW(6)),
-- A004: straight line 8000-800 over 60m = 120.00/m; three months booked, now in repair.
(4, 'A004', 'Cisco Catalyst 2960',   'Network',    'UNDER_REPAIR',  8000.00,  800.00, DATE '2025-09-10', 60, 3, 'SL',   7640.00, 120.00,     NULL,       NULL, '2025-12', 2, NOW(6), NOW(6)),
-- A005: spare unit sitting in stock.
(5, 'A005', 'Lenovo ThinkSystem SR', 'DataCenter', 'IN_STOCK',     60000.00, 6000.00, NULL,              NULL, 0, 'SL',  60000.00, NULL,       NULL,       NULL, NULL,       0, NOW(6), NOW(6));

INSERT INTO asset_transition (asset_id, request_id, from_status, to_status, effective_period, reason, expected_version, resulted_version, created_at) VALUES
(1, 'seed-1-activate', NULL,     'IN_USE',       '2025-07', 'Initial deployment',   0, 1, NOW(6)),
(2, 'seed-2-activate', NULL,     'IN_USE',       '2025-10', 'Initial deployment',   0, 1, NOW(6)),
(4, 'seed-4-activate', NULL,     'IN_USE',       '2025-09', 'Initial deployment',   0, 1, NOW(6)),
(4, 'seed-4-repair',   'IN_USE', 'UNDER_REPAIR', '2026-08', 'Power supply failure', 1, 2, NOW(6));

-- Seeded booked ledger entries (all within the closed period, immutable).
INSERT INTO depreciation_entry (asset_id, period, method, opening_nbv, charge, closing_nbv, run_id, posted_at) VALUES
(1, '2025-08', 'SL', 12000.00, 300.00, 11700.00, NULL, NOW(6)),
(1, '2025-09', 'SL', 11700.00, 300.00, 11400.00, NULL, NOW(6)),
(1, '2025-10', 'SL', 11400.00, 300.00, 11100.00, NULL, NOW(6)),
(1, '2025-11', 'SL', 11100.00, 300.00, 10800.00, NULL, NOW(6)),
(1, '2025-12', 'SL', 10800.00, 300.00, 10500.00, NULL, NOW(6)),
(2, '2025-11', 'DDB', 24000.00, 1000.00, 23000.00, NULL, NOW(6)),
(2, '2025-12', 'DDB', 23000.00, 958.33, 22041.67, NULL, NOW(6)),
(4, '2025-10', 'SL', 8000.00, 120.00, 7880.00, NULL, NOW(6)),
(4, '2025-11', 'SL', 7880.00, 120.00, 7760.00, NULL, NOW(6)),
(4, '2025-12', 'SL', 7760.00, 120.00, 7640.00, NULL, NOW(6));

INSERT INTO period_close (period, closed_by, closed_at, note) VALUES
('2025-12', 'finance', NOW(6), 'Seed: December 2025 month-end close');
