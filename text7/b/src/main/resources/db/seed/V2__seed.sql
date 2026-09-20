-- V2: 样例数据（演示用）

INSERT INTO assets (asset_code, name, purchase_cost, salvage_value, commission_date,
                    useful_life_months, department, status, depreciation_method,
                    pending_book_value_delta, version, created_at, updated_at)
VALUES
    ('LT-0001', 'ThinkPad 笔记本电脑', 12000.00, 600.00, '2026-06-01', 36, '研发部', 'IN_USE', 'STRAIGHT_LINE', 0.00, 0, NOW(6), NOW(6)),
    ('SV-0001', '机架式服务器',       80000.00, 4000.00, '2026-05-15', 60, '运维部', 'IN_USE', 'DECLINING_BALANCE', 0.00, 0, NOW(6), NOW(6)),
    ('PC-0001', '前台台式机',          5000.00, 200.00, '2026-07-01', 36, '行政部', 'IN_STOCK', 'STRAIGHT_LINE', 0.00, 0, NOW(6), NOW(6)),
    ('PR-0001', '激光打印机',          3000.00, 150.00, '2025-01-10', 36, '行政部', 'UNDER_REPAIR', 'STRAIGHT_LINE', 0.00, 0, NOW(6), NOW(6));

INSERT INTO fiscal_periods (period, status) VALUES
    ('2026-07', 'OPEN'),
    ('2026-08', 'OPEN'),
    ('2026-09', 'OPEN');
