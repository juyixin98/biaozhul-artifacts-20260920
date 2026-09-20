-- 企业 IT 资产生命周期：初始 schema
-- 金额一律 DECIMAL(18,4) 定点数；月度入账额按分（2 位小数）四舍五入。

CREATE TABLE asset (
    id                     BIGINT       NOT NULL AUTO_INCREMENT,
    asset_code             VARCHAR(64)  NOT NULL,
    asset_name             VARCHAR(200) NOT NULL,
    purchase_cost          DECIMAL(18,4) NOT NULL,
    salvage_value          DECIMAL(18,4) NOT NULL,
    in_service_date        DATE         NOT NULL COMMENT '启用日期，折旧自次月起计提',
    useful_life_months     INT          NOT NULL,
    department             VARCHAR(100) NOT NULL,
    depreciation_method    VARCHAR(32)  NOT NULL COMMENT 'STRAIGHT_LINE / DECLINING_BALANCE',
    declining_rate_pct     DECIMAL(10,4) NULL COMMENT '余额递减法年折旧率百分数',
    status                 VARCHAR(32)  NOT NULL DEFAULT 'IN_STOCK'
        COMMENT 'IN_STOCK/IN_USE/UNDER_REPAIR/RETIRED/DISPOSED',
    last_depreciated_period VARCHAR(6)  NULL,
    created_at             DATETIME     NOT NULL,
    version                BIGINT       NOT NULL DEFAULT 0 COMMENT '乐观锁版本',
    PRIMARY KEY (id),
    UNIQUE KEY uk_asset_code (asset_code),
    CONSTRAINT chk_salvage_le_cost CHECK (salvage_value >= 0 AND salvage_value <= purchase_cost),
    CONSTRAINT chk_life CHECK (useful_life_months BETWEEN 1 AND 600),
    CONSTRAINT chk_status CHECK (status IN
        ('IN_STOCK','IN_USE','UNDER_REPAIR','RETIRED','DISPOSED')),
    CONSTRAINT chk_method CHECK (depreciation_method IN
        ('STRAIGHT_LINE','DECLINING_BALANCE')),
    CONSTRAINT chk_db_rate CHECK (
        depreciation_method = 'STRAIGHT_LINE'
        OR (declining_rate_pct > 0 AND declining_rate_pct < 100))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 仅追加：状态转换记录
CREATE TABLE status_change (
    id               BIGINT      NOT NULL AUTO_INCREMENT,
    asset_id         BIGINT      NOT NULL,
    from_status      VARCHAR(32) NULL,
    to_status        VARCHAR(32) NOT NULL,
    expected_version BIGINT      NOT NULL COMMENT '请求携带的预期版本',
    request_id       VARCHAR(64) NOT NULL COMMENT '幂等请求 ID',
    reason           VARCHAR(500) NULL,
    changed_by       VARCHAR(100) NOT NULL,
    created_at       DATETIME    NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_status_change_request (request_id),
    KEY idx_status_change_asset (asset_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 仅追加：月度折旧分录（按资产+期间唯一，重跑不重复记账）
CREATE TABLE depreciation_entry (
    id                    BIGINT        NOT NULL AUTO_INCREMENT,
    asset_id              BIGINT        NOT NULL,
    period                VARCHAR(6)       NOT NULL COMMENT '会计期间 yyyyMM',
    depreciation_method   VARCHAR(32)   NOT NULL,
    monthly_rate_pct      DECIMAL(10,6) NULL,
    opening_book_value    DECIMAL(18,4) NOT NULL COMMENT '期初账面价值',
    depreciation_amount   DECIMAL(18,4) NOT NULL COMMENT '本月计提',
    closing_book_value    DECIMAL(18,4) NOT NULL COMMENT '期末账面价值',
    cost_snapshot         DECIMAL(18,4) NOT NULL COMMENT '计提时成本参数快照',
    life_months_snapshot  INT           NOT NULL COMMENT '计提时使用月数快照',
    period_index          INT           NOT NULL COMMENT '所在折旧段内月序号',
    request_id            VARCHAR(64)   NOT NULL,
    created_at            DATETIME      NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_asset_period (asset_id, period),
    KEY idx_entry_period (period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 仅追加：参数调整留痕（原因 + 旧参数）
CREATE TABLE parameter_adjustment (
    id                        BIGINT        NOT NULL AUTO_INCREMENT,
    asset_id                  BIGINT        NOT NULL,
    effective_period          VARCHAR(6)       NOT NULL,
    old_purchase_cost         DECIMAL(18,4) NULL,
    new_purchase_cost         DECIMAL(18,4) NULL,
    old_salvage_value         DECIMAL(18,4) NULL,
    new_salvage_value         DECIMAL(18,4) NULL,
    old_useful_life_months    INT           NULL,
    new_useful_life_months    INT           NULL,
    old_depreciation_method   VARCHAR(32)   NULL,
    new_depreciation_method   VARCHAR(32)   NULL,
    old_declining_rate_pct    DECIMAL(10,4) NULL,
    new_declining_rate_pct    DECIMAL(10,4) NULL,
    book_value_at_adjustment  DECIMAL(18,4) NOT NULL,
    elapsed_months            INT           NOT NULL,
    segment_months            INT           NOT NULL,
    reason                    VARCHAR(500)  NOT NULL,
    adjusted_by               VARCHAR(100)  NOT NULL,
    request_id                VARCHAR(64)   NOT NULL,
    created_at                DATETIME      NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_adjustment_request (request_id),
    KEY idx_adjustment_asset (asset_id, effective_period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 关账标记（关账后该期间及更早期间冻结）
CREATE TABLE period_close (
    period     VARCHAR(6)     NOT NULL,
    closed_by  VARCHAR(100) NOT NULL,
    closed_at  DATETIME    NOT NULL,
    PRIMARY KEY (period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 仅追加：折旧运行请求（requestId 幂等）
CREATE TABLE depreciation_run (
    id           BIGINT      NOT NULL AUTO_INCREMENT,
    request_id   VARCHAR(64) NOT NULL,
    asset_id     BIGINT      NOT NULL,
    from_period  VARCHAR(6)     NOT NULL,
    to_period    VARCHAR(6)     NOT NULL,
    created_at   DATETIME    NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_dep_run_request (request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 期间互斥量：折旧计提与关账均先 INSERT IGNORE 再 SELECT ... FOR UPDATE，
-- 使同期间上的"计提 vs 关账"严格串行（只靠 period_close 间隙锁无法跨表互斥）。
CREATE TABLE period_mutex (
    period VARCHAR(6) NOT NULL,
    PRIMARY KEY (period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 数据库层强制"仅追加"：任何 UPDATE/DELETE 直接拒绝（单语句触发器，免 DELIMITER）。
CREATE TRIGGER trg_status_change_no_update
BEFORE UPDATE ON status_change FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'status_change 为仅追加记录表，禁止 UPDATE';

CREATE TRIGGER trg_status_change_no_delete
BEFORE DELETE ON status_change FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'status_change 为仅追加记录表，禁止 DELETE';

CREATE TRIGGER trg_dep_entry_no_update
BEFORE UPDATE ON depreciation_entry FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'depreciation_entry 为仅追加记录表，禁止 UPDATE';

CREATE TRIGGER trg_dep_entry_no_delete
BEFORE DELETE ON depreciation_entry FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'depreciation_entry 为仅追加记录表，禁止 DELETE';

CREATE TRIGGER trg_adjustment_no_update
BEFORE UPDATE ON parameter_adjustment FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'parameter_adjustment 为仅追加记录表，禁止 UPDATE';

CREATE TRIGGER trg_adjustment_no_delete
BEFORE DELETE ON parameter_adjustment FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'parameter_adjustment 为仅追加记录表，禁止 DELETE';

CREATE TRIGGER trg_dep_run_no_update
BEFORE UPDATE ON depreciation_run FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'depreciation_run 为仅追加记录表，禁止 UPDATE';

CREATE TRIGGER trg_dep_run_no_delete
BEFORE DELETE ON depreciation_run FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'depreciation_run 为仅追加记录表，禁止 DELETE';
