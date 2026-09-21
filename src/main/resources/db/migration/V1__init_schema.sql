-- 企业 IT 资产生命周期 - 初始 schema
-- 所有金额使用定点数 DECIMAL(18,2)，单位元，会计期间为整数 YYYYMM（如 202609）。

CREATE TABLE app_user (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    username      VARCHAR(64)  NOT NULL,
    password_hash VARCHAR(100) NOT NULL,
    role          VARCHAR(24)  NOT NULL COMMENT 'FINANCE / MANAGER / VIEWER',
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_username (username)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

CREATE TABLE asset (
    id                BIGINT        NOT NULL AUTO_INCREMENT,
    asset_code        VARCHAR(64)   NOT NULL,
    name              VARCHAR(200)  NOT NULL,
    department        VARCHAR(100)  NOT NULL,
    purchase_cost     DECIMAL(18,2) NOT NULL COMMENT '采购成本',
    salvage_value     DECIMAL(18,2) NOT NULL COMMENT '残值',
    placed_in_service DATE          NULL COMMENT '启用日期；未启用的库存资产可为空',
    useful_life_months INT          NOT NULL COMMENT '预计使用月数',
    depreciation_method VARCHAR(24) NOT NULL COMMENT 'STRAIGHT_LINE / DECLINING_BALANCE',
    status            VARCHAR(24)   NOT NULL COMMENT 'IN_STOCK/IN_USE/IN_REPAIR/RETIRED/DISPOSED',
    version           BIGINT        NOT NULL DEFAULT 0 COMMENT '乐观锁版本',
    created_at        DATETIME(6)   NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_asset_code (asset_code),
    CONSTRAINT ck_salvage_le_cost CHECK (salvage_value >= 0 AND salvage_value <= purchase_cost),
    CONSTRAINT ck_life_positive CHECK (useful_life_months > 0)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 仅追加的状态变更记录（不做更新/删除，应用层也不暴露写入接口）
CREATE TABLE status_transition (
    id                   BIGINT       NOT NULL AUTO_INCREMENT,
    asset_id             BIGINT       NOT NULL,
    request_id           VARCHAR(100) NOT NULL COMMENT '客户端请求ID，用于幂等',
    from_status          VARCHAR(24)  NULL COMMENT 'NULL 表示资产建档（初始入库）',
    to_status            VARCHAR(24)  NOT NULL,
    expected_version     BIGINT       NOT NULL,
    note                 VARCHAR(500) NULL,
    operator             VARCHAR(64)  NOT NULL,
    created_at           DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_transition_request (request_id),
    KEY idx_transition_asset (asset_id, id),
    CONSTRAINT fk_transition_asset FOREIGN KEY (asset_id) REFERENCES asset (id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 会计期间：仅当被显式关闭时才存在一行（closed=TRUE）；未关账期间无需建期
CREATE TABLE accounting_period (
    id         BIGINT      NOT NULL AUTO_INCREMENT,
    period     INT         NOT NULL,
    closed     BOOLEAN     NOT NULL DEFAULT TRUE,
    closed_by  VARCHAR(64) NOT NULL,
    closed_at  DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_period (period)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 折旧政策链：asset 建档时写入一条；每次可追溯调整追加一条，旧参数原样保留
CREATE TABLE depreciation_policy (
    id                    BIGINT        NOT NULL AUTO_INCREMENT,
    asset_id              BIGINT        NOT NULL,
    sequence_no           INT           NOT NULL COMMENT '该资产上的政策序号，从 1 开始',
    effective_period      INT           NOT NULL COMMENT '生效期间 YYYYMM',
    purchase_cost         DECIMAL(18,2) NOT NULL COMMENT '该政策下成本（调整时记录新值）',
    salvage_value         DECIMAL(18,2) NOT NULL COMMENT '该政策下残值',
    useful_life_months    INT           NOT NULL COMMENT '该政策下使用月数',
    depreciation_method   VARCHAR(24)   NOT NULL,
    opening_book_value    DECIMAL(18,2) NOT NULL COMMENT '生效期初账面价值（未来适用法依据）',
    remaining_life_months INT           NOT NULL COMMENT '生效时点剩余可计提月数',
    reason                VARCHAR(500)  NULL COMMENT '调整原因；建档政策为 null',
    adjusted_by           VARCHAR(64)   NULL,
    created_at            DATETIME(6)   NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_policy_asset_seq (asset_id, sequence_no),
    KEY idx_policy_asset_effective (asset_id, effective_period),
    CONSTRAINT fk_policy_asset FOREIGN KEY (asset_id) REFERENCES asset (id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 折旧分录：资产 + 期间唯一（DB 约束保证重跑不重复记账）
CREATE TABLE depreciation_entry (
    id              BIGINT        NOT NULL AUTO_INCREMENT,
    asset_id        BIGINT        NOT NULL,
    period          INT           NOT NULL,
    opening_value   DECIMAL(18,2) NOT NULL COMMENT '期初账面价值',
    charge          DECIMAL(18,2) NOT NULL COMMENT '本月计提，HALF_UP 保留两位',
    closing_value   DECIMAL(18,2) NOT NULL COMMENT '期末账面价值 = 期初 - 计提',
    policy_id       BIGINT        NOT NULL COMMENT '计算所用政策版本',
    calc_detail     VARCHAR(1000) NOT NULL COMMENT '计算口径说明（可解释）',
    created_at      DATETIME(6)   NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_entry_asset_period (asset_id, period),
    KEY idx_entry_period (period),
    CONSTRAINT fk_entry_asset FOREIGN KEY (asset_id) REFERENCES asset (id),
    CONSTRAINT fk_entry_policy FOREIGN KEY (policy_id) REFERENCES depreciation_policy (id),
    CONSTRAINT ck_charge_nonneg CHECK (charge >= 0),
    CONSTRAINT ck_closing_ge_salvage CHECK (closing_value >= 0)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;
