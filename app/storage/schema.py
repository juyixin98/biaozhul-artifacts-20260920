"""SQLite 模式定义（schema v1）。

约束是正确性的最后一道防线：即使应用层有并发缺陷，数据库的
UNIQUE/CHECK 约束也会阻止同一研究内重复入组或同一槽位被占两次。
"""
from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studies (
    study_id           TEXT PRIMARY KEY,
    contract_json      TEXT NOT NULL,
    fingerprint        TEXT NOT NULL UNIQUE,
    seed_proof         TEXT NOT NULL,
    spec_version       TEXT NOT NULL,
    contract_version   INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    sealed             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS strata (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id           TEXT NOT NULL REFERENCES studies(study_id),
    stratum_key        TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'sealed')),
    sealed_at          TEXT,
    UNIQUE (study_id, stratum_key)
);

CREATE TABLE IF NOT EXISTS blocks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id           TEXT NOT NULL REFERENCES studies(study_id),
    stratum_key        TEXT NOT NULL,
    block_index        INTEGER NOT NULL,
    block_size         INTEGER NOT NULL,
    -- 完整置换*默认不落盘*：开放区组的未来次序一旦写库，有库权限者即可
    -- 预知分配，破坏分配隐藏。置换是 (种子, 研究, 层, 区组号) 的纯函数，
    -- 需要时由保险库中的种子重算。区组封闭/排满后可由审计导出留档。
    perm_json          TEXT,
    slot_arms_json     TEXT NOT NULL,   -- 每个槽位对应的臂（只反映比例，不含次序）
    filled             INTEGER NOT NULL DEFAULT 0 CHECK (filled <= block_size),
    status             TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'full', 'sealed_tail')),
    UNIQUE (study_id, stratum_key, block_index)
);

CREATE TABLE IF NOT EXISTS allocations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id           TEXT NOT NULL REFERENCES studies(study_id),
    subject_id         TEXT NOT NULL,
    stratum_key        TEXT NOT NULL,
    block_id           INTEGER NOT NULL REFERENCES blocks(id),
    position           INTEGER NOT NULL,
    arm                TEXT NOT NULL,
    features_digest    TEXT NOT NULL,
    features_canonical BLOB NOT NULL,
    idempotency_key    TEXT,
    request_id         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    UNIQUE (study_id, subject_id),
    UNIQUE (study_id, idempotency_key),
    UNIQUE (block_id, position)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   TEXT NOT NULL,
    request_id           TEXT NOT NULL,
    study_id             TEXT,
    subject_id           TEXT,
    actor_role           TEXT NOT NULL,
    action               TEXT NOT NULL,
    outcome              TEXT NOT NULL,
        -- replayed(幂等回放) / committed(新提交) / rejected / uncertain
    category             TEXT,
    contract_fingerprint TEXT,
    stream_locators_json TEXT,
    detail_json          TEXT,
    spec_version         TEXT
);

CREATE INDEX IF NOT EXISTS idx_alloc_study ON allocations(study_id);
CREATE INDEX IF NOT EXISTS idx_blocks_study_stratum
    ON blocks(study_id, stratum_key);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_events(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_study_ts ON audit_events(study_id, id);
"""
