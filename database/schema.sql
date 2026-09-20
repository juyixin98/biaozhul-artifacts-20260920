-- Meridian TravelOps contract settlement schema
-- MySQL 8, InnoDB, amounts are DECIMAL (fixed-point), never floats.

SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE IF NOT EXISTS contract_templates (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    code            VARCHAR(64)  NOT NULL,
    name            VARCHAR(255) NOT NULL,
    body_template   MEDIUMTEXT   NOT NULL,
    version         INT UNSIGNED NOT NULL DEFAULT 1,
    created_by      VARCHAR(128) NOT NULL,
    created_at      DATETIME(6)  NOT NULL,
    updated_at      DATETIME(6)  NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_template_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS contracts (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    code             VARCHAR(64)  NOT NULL,
    customer_name    VARCHAR(255) NOT NULL,
    status           VARCHAR(16)  NOT NULL DEFAULT 'draft',
                     -- draft | pending | active | withdrawn | expired
    current_version_id BIGINT UNSIGNED NULL,
    created_by       VARCHAR(128) NOT NULL,
    created_at       DATETIME(6)  NOT NULL,
    updated_at       DATETIME(6)  NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_contract_code (code),
    KEY idx_contract_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS contract_versions (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    contract_id      BIGINT UNSIGNED NOT NULL,
    version_no       INT UNSIGNED NOT NULL,
    template_id      BIGINT UNSIGNED NOT NULL,
    variables_json   JSON NOT NULL,
    frozen_content   MEDIUMTEXT NOT NULL,       -- complete rendered content, frozen
    content_hash     CHAR(64) NOT NULL,         -- sha256 of canonical frozen content
    summary_json     JSON NOT NULL,
    signer_order     JSON NOT NULL,             -- ["alice","bob",...]
    status           VARCHAR(16) NOT NULL,      -- pending|signed|withdrawn|expired
    expires_at       DATETIME(6) NOT NULL,      -- initiated_at + 72h
    initiated_by     VARCHAR(128) NOT NULL,
    initiated_at     DATETIME(6) NOT NULL,
    completed_at     DATETIME(6) NULL,
    withdrawn_at     DATETIME(6) NULL,
    expired_at       DATETIME(6) NULL,
    created_at       DATETIME(6) NOT NULL,
    updated_at       DATETIME(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_version (contract_id, version_no),
    KEY idx_cv_status (status),
    KEY idx_cv_expires (status, expires_at),
    CONSTRAINT fk_cv_contract FOREIGN KEY (contract_id) REFERENCES contracts (id),
    CONSTRAINT fk_cv_template FOREIGN KEY (template_id) REFERENCES contract_templates (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS signatures (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    contract_version_id BIGINT UNSIGNED NOT NULL,
    signer           VARCHAR(128) NOT NULL,
    position         INT UNSIGNED NOT NULL,
    content_hash     CHAR(64) NOT NULL,         -- hash the signer confirmed
    content_version  INT UNSIGNED NOT NULL,     -- contract_versions.version_no confirmed
    status           VARCHAR(16) NOT NULL DEFAULT 'signed',
    signed_by        VARCHAR(128) NOT NULL,
    signed_at        DATETIME(6) NOT NULL,
    created_at       DATETIME(6) NOT NULL,
    updated_at       DATETIME(6) NULL,
    PRIMARY KEY (id),
    -- each signer can confirm a given version exactly once: retries cannot double-confirm
    UNIQUE KEY uq_sig (contract_version_id, signer),
    KEY idx_sig_order (contract_version_id, position),
    CONSTRAINT fk_sig_version FOREIGN KEY (contract_version_id)
        REFERENCES contract_versions (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS allocation_rule_versions (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    contract_id      BIGINT UNSIGNED NOT NULL,
    rule_version     INT UNSIGNED NOT NULL,
    rules_json       JSON NOT NULL,
                     -- [{"target":"hotel-a","weight":70,"remainder_owner":true}, ...]
    status           VARCHAR(16) NOT NULL DEFAULT 'active', -- active|superseded
    created_by       VARCHAR(128) NOT NULL,
    created_at       DATETIME(6) NOT NULL,
    updated_at       DATETIME(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_rule_version (contract_id, rule_version),
    KEY idx_rule_status (contract_id, status),
    CONSTRAINT fk_rule_contract FOREIGN KEY (contract_id) REFERENCES contracts (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS import_batches (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    batch_ref        VARCHAR(64) NULL,
    total_count      INT UNSIGNED NOT NULL DEFAULT 0,
    total_amount_json JSON NULL,                -- {"EUR":"1000.0000", ...}
    status           VARCHAR(16) NOT NULL DEFAULT 'committed',
    created_by       VARCHAR(128) NOT NULL,
    created_at       DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_batch_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS settlement_entries (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    contract_id      BIGINT UNSIGNED NOT NULL,
    currency         CHAR(3) NOT NULL,
    business_date    DATE NOT NULL,
    external_txn_id  VARCHAR(128) NOT NULL,
    amount           DECIMAL(18,4) NOT NULL,
    description      VARCHAR(500) NULL,
    source           VARCHAR(16) NOT NULL DEFAULT 'import', -- import|reversal|adjustment
    import_batch_id  BIGINT UNSIGNED NULL,
    linked_entry_id  BIGINT UNSIGNED NULL,     -- reversal/adjustment -> original entry
    created_by       VARCHAR(128) NOT NULL,
    created_at       DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_ext_txn (external_txn_id),
    KEY idx_entry_agg (contract_id, currency, business_date),
    KEY idx_entry_link (linked_entry_id),
    CONSTRAINT fk_entry_contract FOREIGN KEY (contract_id) REFERENCES contracts (id),
    CONSTRAINT fk_entry_batch FOREIGN KEY (import_batch_id) REFERENCES import_batches (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS daily_closes (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    business_date    DATE NOT NULL,
    status           VARCHAR(16) NOT NULL DEFAULT 'closed',
    entry_count      INT UNSIGNED NOT NULL,
    total_amount_json JSON NOT NULL,
    closed_by        VARCHAR(128) NOT NULL,
    closed_at        DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_close_date (business_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS settlements (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    contract_id      BIGINT UNSIGNED NOT NULL,
    contract_version_id BIGINT UNSIGNED NOT NULL, -- bound to a fully signed version
    rule_version_id  BIGINT UNSIGNED NOT NULL,
    currency         CHAR(3) NOT NULL,
    business_date    DATE NOT NULL,
    amount           DECIMAL(18,4) NOT NULL,
    content_hash     CHAR(64) NOT NULL,
    idempotency_key  VARCHAR(128) NOT NULL,
    created_by       VARCHAR(128) NOT NULL,
    created_at       DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_settle_idem (idempotency_key),
    KEY idx_settle_agg (contract_id, currency, business_date),
    CONSTRAINT fk_settle_contract FOREIGN KEY (contract_id) REFERENCES contracts (id),
    CONSTRAINT fk_settle_version FOREIGN KEY (contract_version_id)
        REFERENCES contract_versions (id),
    CONSTRAINT fk_settle_rules FOREIGN KEY (rule_version_id)
        REFERENCES allocation_rule_versions (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS settlement_allocations (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    settlement_id    BIGINT UNSIGNED NOT NULL,
    target           VARCHAR(128) NOT NULL,
    weight           INT UNSIGNED NOT NULL,
    allocated_amount DECIMAL(18,4) NOT NULL,
    is_remainder_owner TINYINT(1) NOT NULL DEFAULT 0,
    position         INT UNSIGNED NOT NULL,
    created_at       DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_alloc_settlement (settlement_id),
    CONSTRAINT fk_alloc_settlement FOREIGN KEY (settlement_id)
        REFERENCES settlements (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS settlement_entry_links (
    settlement_id    BIGINT UNSIGNED NOT NULL,
    entry_id         BIGINT UNSIGNED NOT NULL,
    amount           DECIMAL(18,4) NOT NULL,
    PRIMARY KEY (settlement_id, entry_id),
    UNIQUE KEY uq_entry_settled (entry_id),  -- an entry is settled once only
    CONSTRAINT fk_sel_settlement FOREIGN KEY (settlement_id)
        REFERENCES settlements (id) ON DELETE CASCADE,
    CONSTRAINT fk_sel_entry FOREIGN KEY (entry_id)
        REFERENCES settlement_entries (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET FOREIGN_KEY_CHECKS = 1;
