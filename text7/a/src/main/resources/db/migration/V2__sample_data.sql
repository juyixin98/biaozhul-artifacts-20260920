-- 样例数据（演示与手工核对用）
--
-- 资产 A（直线法）：成本 12000，残值 0，使用 12 月，2025-09-15 启用，
--   首个折旧期间 202510，月计提 1000.00；已计提 202510..202602（5 期，账面 7000）。
-- 资产 B（余额递减法，年率 40%）：成本 6000，残值 500，使用 24 月，
--   2025-12-20 启用，首个期间 202601；月率 3.333333%：
--   202601 计提 200.00（6000.00 -> 5800.00），
--   202602 计提 193.33（5800.00 -> 5606.67）。
-- 资产 C：库存状态的笔记本，尚未计提；资产 D：维修中的服务器；资产 E：已退役待处置。

INSERT INTO asset (id, asset_code, asset_name, purchase_cost, salvage_value, in_service_date,
                   useful_life_months, department, depreciation_method, declining_rate_pct,
                   status, last_depreciated_period, created_at, version)
VALUES
 (1,'NB-2025-001','财务部 ThinkPad X1',12000.0000,0.0000,'2025-09-15',12,'财务部',
  'STRAIGHT_LINE',NULL,'IN_USE','202602','2025-09-15 09:00:00',1),
 (2,'SV-2025-002','研发部 Dell R750 服务器',6000.0000,500.0000,'2025-12-20',24,'研发部',
  'DECLINING_BALANCE',40.0000,'IN_USE','202602','2025-12-20 09:00:00',1),
 (3,'NB-2026-003','市场部 MacBook Pro',18000.0000,1800.0000,'2026-01-10',36,'市场部',
  'STRAIGHT_LINE',NULL,'IN_STOCK',NULL,'2026-01-10 09:00:00',0),
 (4,'SV-2026-004','运维部 HP DL380',9000.0000,900.0000,'2026-01-05',24,'运维部',
  'DECLINING_BALANCE',50.0000,'UNDER_REPAIR',NULL,'2026-01-05 09:00:00',2),
 (5,'PR-2025-005','行政部打印机',3000.0000,0.0000,'2025-08-01',12,'行政部',
  'STRAIGHT_LINE',NULL,'RETIRED',NULL,'2025-08-01 09:00:00',2);

INSERT INTO status_change (id, asset_id, from_status, to_status, expected_version, request_id,
                           reason, changed_by, created_at)
VALUES
 (1,1,'IN_STOCK','IN_USE',0,'seed-nb001-activate','发放给财务人员','manager','2025-09-16 10:00:00'),
 (2,4,'IN_STOCK','IN_USE',0,'seed-sv004-activate','上线','manager','2026-01-06 10:00:00'),
 (3,4,'IN_USE','UNDER_REPAIR',1,'seed-sv004-repair','主板故障返修','manager','2026-02-12 14:00:00'),
 (4,5,'IN_STOCK','IN_USE',0,'seed-pr005-activate','行政使用','manager','2025-08-02 10:00:00'),
 (5,5,'IN_USE','RETIRED',1,'seed-pr005-retire','老化退役','manager','2026-02-20 16:00:00');

-- 资产 A 直线法 5 期：每期 1000.00
INSERT INTO depreciation_entry
 (asset_id, period, depreciation_method, monthly_rate_pct, opening_book_value,
  depreciation_amount, closing_book_value, cost_snapshot, life_months_snapshot,
  period_index, request_id, created_at)
VALUES
 (1,'202510','STRAIGHT_LINE',NULL,12000.0000,1000.0000,11000.0000,12000.0000,12,1,'seed-a-202510','2025-11-01 02:00:00'),
 (1,'202511','STRAIGHT_LINE',NULL,11000.0000,1000.0000,10000.0000,12000.0000,12,2,'seed-a-202511','2025-12-01 02:00:00'),
 (1,'202512','STRAIGHT_LINE',NULL,10000.0000,1000.0000,9000.0000,12000.0000,12,3,'seed-a-202512','2026-01-01 02:00:00'),
 (1,'202601','STRAIGHT_LINE',NULL,9000.0000,1000.0000,8000.0000,12000.0000,12,4,'seed-a-202601','2026-02-01 02:00:00'),
 (1,'202602','STRAIGHT_LINE',NULL,8000.0000,1000.0000,7000.0000,12000.0000,12,5,'seed-a-202602','2026-03-01 02:00:00'),
-- 资产 B 余额递减法 2 期
 (2,'202601','DECLINING_BALANCE',3.333333,6000.0000,200.0000,5800.0000,6000.0000,24,1,'seed-b-202601','2026-02-01 02:00:00'),
 (2,'202602','DECLINING_BALANCE',3.333333,5800.0000,193.3300,5606.6700,6000.0000,24,2,'seed-b-202602','2026-03-01 02:00:00');
