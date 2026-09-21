-- IT asset lifecycle schema. Money columns are DECIMAL (fixed-point), never float.
-- Periods are calendar months stored as 'YYYY-MM' (lexicographic order == chronological order).

CREATE TABLE app_user (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    username      VARCHAR(64)  NOT NULL,
    password_hash VARCHAR(100) NOT NULL,
    role          VARCHAR(20)  NOT NULL,
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    DATETIME(6)  NOT NULL,
    CONSTRAINT pk_app_user PRIMARY KEY (id),
    CONSTRAINT uk_app_user_username UNIQUE (username),
    CONSTRAINT chk_app_user_role CHECK (role IN ('FINANCE', 'ASSET_MANAGER', 'VIEWER'))
) ENGINE = InnoDB;

CREATE TABLE hardware_asset (
    id                   BIGINT        NOT NULL AUTO_INCREMENT,
    asset_code           VARCHAR(40)   NOT NULL,
    name                 VARCHAR(200)  NOT NULL,
    department           VARCHAR(100)  NOT NULL,
    status               VARCHAR(20)   NOT NULL,
    cost                 DECIMAL(18,2) NOT NULL,
    salvage_value        DECIMAL(18,2) NOT NULL,
    in_service_date      DATE          NULL,
    useful_life_months   INT           NULL,
    used_months          INT           NOT NULL DEFAULT 0,
    method               VARCHAR(10)   NOT NULL DEFAULT 'SL',
    nbv                  DECIMAL(18,2) NOT NULL,
    sl_monthly           DECIMAL(18,2) NULL,
    ddb_rate             DECIMAL(12,8) NULL,
    exit_period          VARCHAR(7)    NULL,
    last_posted_period   VARCHAR(7)    NULL,
    version              BIGINT        NOT NULL DEFAULT 0,
    created_at           DATETIME(6)   NOT NULL,
    updated_at           DATETIME(6)   NOT NULL,
    CONSTRAINT pk_hardware_asset PRIMARY KEY (id),
    CONSTRAINT uk_asset_code UNIQUE (asset_code),
    CONSTRAINT chk_asset_status CHECK (status IN ('IN_STOCK', 'IN_USE', 'UNDER_REPAIR', 'RETIRED', 'DISPOSED')),
    CONSTRAINT chk_asset_method CHECK (method IN ('SL', 'DDB')),
    CONSTRAINT chk_asset_amounts CHECK (
        cost >= 0 AND salvage_value >= 0 AND salvage_value <= cost
        AND nbv >= salvage_value AND nbv <= cost),
    CONSTRAINT chk_asset_life CHECK (useful_life_months IS NULL OR useful_life_months > 0),
    CONSTRAINT chk_asset_used_months CHECK (used_months >= 0)
) ENGINE = InnoDB;

CREATE TABLE asset_transition (
    id               BIGINT       NOT NULL AUTO_INCREMENT,
    asset_id         BIGINT       NOT NULL,
    request_id       VARCHAR(64)  NOT NULL,
    from_status      VARCHAR(20)  NULL,
    to_status        VARCHAR(20)  NOT NULL,
    effective_period VARCHAR(7)   NOT NULL,
    reason           VARCHAR(500) NULL,
    expected_version BIGINT       NOT NULL,
    resulted_version BIGINT       NOT NULL,
    created_at       DATETIME(6)  NOT NULL,
    CONSTRAINT pk_asset_transition PRIMARY KEY (id),
    CONSTRAINT uk_transition_request UNIQUE (request_id),
    CONSTRAINT fk_transition_asset FOREIGN KEY (asset_id) REFERENCES hardware_asset (id),
    CONSTRAINT chk_transition_to CHECK (to_status IN ('IN_STOCK', 'IN_USE', 'UNDER_REPAIR', 'RETIRED', 'DISPOSED'))
) ENGINE = InnoDB;
CREATE INDEX idx_transition_asset ON asset_transition (asset_id);

CREATE TABLE depreciation_entry (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    asset_id    BIGINT        NOT NULL,
    period      VARCHAR(7)    NOT NULL,
    method      VARCHAR(10)   NOT NULL,
    opening_nbv DECIMAL(18,2) NOT NULL,
    charge      DECIMAL(18,2) NOT NULL,
    closing_nbv DECIMAL(18,2) NOT NULL,
    run_id      BIGINT       NULL,
    posted_at   DATETIME(6)   NOT NULL,
    CONSTRAINT pk_depreciation_entry PRIMARY KEY (id),
    CONSTRAINT uk_entry_asset_period UNIQUE (asset_id, period),
    CONSTRAINT fk_entry_asset FOREIGN KEY (asset_id) REFERENCES hardware_asset (id),
    CONSTRAINT chk_entry_method CHECK (method IN ('SL', 'DDB')),
    CONSTRAINT chk_entry_amounts CHECK (charge >= 0 AND closing_nbv >= 0)
) ENGINE = InnoDB;
CREATE INDEX idx_entry_period ON depreciation_entry (period);

CREATE TABLE depreciation_run (
    id             BIGINT        NOT NULL AUTO_INCREMENT,
    request_id     VARCHAR(64)   NOT NULL,
    period         VARCHAR(7)    NOT NULL,
    status         VARCHAR(20)   NOT NULL,
    assets_posted  INT           NOT NULL DEFAULT 0,
    entries_created INT          NOT NULL DEFAULT 0,
    total_charge   DECIMAL(18,2) NOT NULL DEFAULT 0,
    message        VARCHAR(2000) NULL,
    started_at     DATETIME(6)   NOT NULL,
    finished_at    DATETIME(6)   NULL,
    CONSTRAINT pk_depreciation_run PRIMARY KEY (id),
    CONSTRAINT uk_run_request UNIQUE (request_id),
    CONSTRAINT chk_run_status CHECK (status IN ('COMPLETED', 'SKIPPED'))
) ENGINE = InnoDB;
CREATE INDEX idx_run_period ON depreciation_run (period);

CREATE TABLE asset_adjustment (
    id                     BIGINT        NOT NULL AUTO_INCREMENT,
    asset_id               BIGINT        NOT NULL,
    request_id             VARCHAR(64)   NOT NULL,
    effective_period       VARCHAR(7)    NOT NULL,
    reason                 VARCHAR(1000) NOT NULL,
    old_cost               DECIMAL(18,2) NOT NULL,
    new_cost               DECIMAL(18,2) NOT NULL,
    old_salvage_value      DECIMAL(18,2) NOT NULL,
    new_salvage_value      DECIMAL(18,2) NOT NULL,
    old_useful_life_months INT           NOT NULL,
    new_useful_life_months INT           NOT NULL,
    old_method             VARCHAR(10)   NOT NULL,
    new_method             VARCHAR(10)   NOT NULL,
    old_sl_monthly         DECIMAL(18,2) NULL,
    new_sl_monthly         DECIMAL(18,2) NULL,
    old_ddb_rate           DECIMAL(12,8) NULL,
    new_ddb_rate           DECIMAL(12,8) NULL,
    open_entries_deleted   INT           NOT NULL DEFAULT 0,
    created_at             DATETIME(6)   NOT NULL,
    CONSTRAINT pk_asset_adjustment PRIMARY KEY (id),
    CONSTRAINT uk_adjustment_request UNIQUE (request_id),
    CONSTRAINT fk_adjustment_asset FOREIGN KEY (asset_id) REFERENCES hardware_asset (id)
) ENGINE = InnoDB;
CREATE INDEX idx_adjustment_asset ON asset_adjustment (asset_id);

CREATE TABLE period_close (
    period    VARCHAR(7)  NOT NULL,
    closed_by VARCHAR(64) NOT NULL,
    closed_at DATETIME(6) NOT NULL,
    note      VARCHAR(500) NULL,
    CONSTRAINT pk_period_close PRIMARY KEY (period)
) ENGINE = InnoDB;

CREATE TABLE idempotent_request (
    request_id    VARCHAR(64)  NOT NULL,
    operation     VARCHAR(40)  NOT NULL,
    fingerprint   VARCHAR(64)  NOT NULL,
    response_code INT          NOT NULL,
    response_body MEDIUMTEXT   NOT NULL,
    created_at    DATETIME(6)  NOT NULL,
    CONSTRAINT pk_idempotent_request PRIMARY KEY (request_id),
    KEY idx_idempotent_operation (operation)
) ENGINE = InnoDB;
