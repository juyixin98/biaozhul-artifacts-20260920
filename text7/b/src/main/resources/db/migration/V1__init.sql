-- V1: 初始 schema。金额一律 decimal(19,2) 定点数。

CREATE TABLE assets (
    id                        BIGINT AUTO_INCREMENT PRIMARY KEY,
    asset_code                VARCHAR(64)  NOT NULL,
    name                      VARCHAR(128) NOT NULL,
    purchase_cost             DECIMAL(19,2) NOT NULL,
    salvage_value             DECIMAL(19,2) NOT NULL,
    commission_date           DATE         NOT NULL,
    useful_life_months        INT          NOT NULL,
    department                VARCHAR(64)  NOT NULL,
    status                    VARCHAR(16)  NOT NULL DEFAULT 'IN_STOCK',
    depreciation_method       VARCHAR(32)  NOT NULL DEFAULT 'STRAIGHT_LINE',
    pending_book_value_delta  DECIMAL(19,2) NOT NULL DEFAULT 0.00,
    version                   BIGINT       NOT NULL DEFAULT 0,
    created_at                DATETIME(6)  NOT NULL,
    updated_at                DATETIME(6)  NOT NULL,
    CONSTRAINT uk_assets_code UNIQUE (asset_code)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- 仅追加的状态变更记录；request_id 唯一支撑幂等
CREATE TABLE asset_status_transitions (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    asset_id         BIGINT      NOT NULL,
    from_status      VARCHAR(16) NOT NULL,
    to_status        VARCHAR(16) NOT NULL,
    request_id       VARCHAR(64) NOT NULL,
    expected_version BIGINT      NOT NULL,
    actor            VARCHAR(64) NOT NULL,
    created_at       DATETIME(6) NOT NULL,
    CONSTRAINT uk_transition_request UNIQUE (request_id),
    CONSTRAINT fk_transition_asset FOREIGN KEY (asset_id) REFERENCES assets (id),
    INDEX idx_transition_asset (asset_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- 折旧明细（仅追加）：(asset_id, period) 唯一，重跑不重复记账
CREATE TABLE depreciation_entries (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    asset_id         BIGINT       NOT NULL,
    period           VARCHAR(7)      NOT NULL,
    opening_value    DECIMAL(19,2) NOT NULL,
    adjustment_delta DECIMAL(19,2) NOT NULL DEFAULT 0.00,
    amount           DECIMAL(19,2) NOT NULL,
    closing_value    DECIMAL(19,2) NOT NULL,
    method           VARCHAR(32)  NOT NULL,
    created_at       DATETIME(6)  NOT NULL,
    CONSTRAINT uk_depreciation_asset_period UNIQUE (asset_id, period),
    CONSTRAINT fk_depreciation_asset FOREIGN KEY (asset_id) REFERENCES assets (id),
    INDEX idx_depreciation_period (period)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- 会计期间：CLOSED 后不可重算
CREATE TABLE fiscal_periods (
    period    VARCHAR(7)     PRIMARY KEY,
    status    VARCHAR(8)  NOT NULL DEFAULT 'OPEN',
    closed_at DATETIME(6) NULL,
    closed_by VARCHAR(64) NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- 可追溯的参数调整记录（仅追加）
CREATE TABLE asset_adjustments (
    id                      BIGINT AUTO_INCREMENT PRIMARY KEY,
    asset_id                BIGINT        NOT NULL,
    old_cost                DECIMAL(19,2) NOT NULL,
    new_cost                DECIMAL(19,2) NOT NULL,
    old_useful_life_months  INT           NOT NULL,
    new_useful_life_months  INT           NOT NULL,
    book_value_delta        DECIMAL(19,2) NOT NULL DEFAULT 0.00,
    reason                  VARCHAR(512)  NOT NULL,
    actor                   VARCHAR(64)   NOT NULL,
    created_at              DATETIME(6)   NOT NULL,
    CONSTRAINT fk_adjustment_asset FOREIGN KEY (asset_id) REFERENCES assets (id),
    INDEX idx_adjustment_asset (asset_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;
