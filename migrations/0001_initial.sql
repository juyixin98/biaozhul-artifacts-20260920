-- 0001 初始结构。与 app/models.py 一一对应。SQLite DDL 可随事务回滚。

CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(200) NOT NULL,
    api_key VARCHAR(64) NOT NULL UNIQUE,
    active_rule_pack_version VARCHAR(200) NOT NULL,
    active_index_generation_id INTEGER,
    created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS blobs (
    sha256 VARCHAR(64) PRIMARY KEY,
    content TEXT NOT NULL,
    byte_length INTEGER NOT NULL DEFAULT 0,
    ref_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_packs (
    version VARCHAR(200) PRIMARY KEY,
    content_json TEXT NOT NULL,
    content_sha256 VARCHAR(64) NOT NULL UNIQUE,
    created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
    name VARCHAR(500) NOT NULL,
    doc_sha256 VARCHAR(64) NOT NULL,
    blob_sha256 VARCHAR(64) REFERENCES blobs(sha256) ON DELETE RESTRICT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    deleted_at DATETIME
);
CREATE INDEX IF NOT EXISTS uq_documents_workspace_sha_alive
    ON documents(workspace_id, doc_sha256) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_documents_workspace_id ON documents(workspace_id);

CREATE TABLE IF NOT EXISTS rule_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    rule_pack_version VARCHAR(200) NOT NULL REFERENCES rule_packs(version),
    action VARCHAR(20) NOT NULL,
    previous_version VARCHAR(200),
    activated_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rule_activations_workspace
    ON rule_activations(workspace_id, activated_at);

CREATE TABLE IF NOT EXISTS worker_registry (
    worker_id VARCHAR(100) PRIMARY KEY,
    heartbeat_at DATETIME NOT NULL,
    started_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    kind VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    rule_pack_version VARCHAR(200) NOT NULL,
    doc_sha256 VARCHAR(64),
    payload_json TEXT,
    priority INTEGER NOT NULL DEFAULT 100,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    error TEXT,
    error_stage VARCHAR(30),
    error_doc_id INTEGER,
    checkpoint_json TEXT,
    leased_by VARCHAR(100),
    leased_until DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    finished_at DATETIME
);
CREATE INDEX IF NOT EXISTS ix_jobs_claim ON jobs(status, kind, priority, id);
CREATE INDEX IF NOT EXISTS ix_jobs_workspace ON jobs(workspace_id);
CREATE INDEX IF NOT EXISTS ix_jobs_document ON jobs(document_id);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    value_json TEXT NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE(workspace_id, name)
);

CREATE TABLE IF NOT EXISTS index_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    rule_pack_version VARCHAR(200) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'building',
    doc_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at DATETIME NOT NULL,
    activated_at DATETIME
);
-- 每个工作区至多一个 active 代：切换原子性的数据库级保证。
CREATE UNIQUE INDEX IF NOT EXISTS uq_index_generations_one_active
    ON index_generations(workspace_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_index_gen_ws_status
    ON index_generations(workspace_id, status);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    rule_pack_version VARCHAR(200) NOT NULL,
    entity_type VARCHAR(20) NOT NULL,
    canonical VARCHAR(500) NOT NULL,
    first_seen_rule_id VARCHAR(100) NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(document_id, rule_pack_version, entity_type, canonical)
);
CREATE INDEX IF NOT EXISTS ix_entities_doc ON entities(document_id);
CREATE INDEX IF NOT EXISTS ix_entities_ws_type_canon
    ON entities(workspace_id, entity_type, canonical);

CREATE TABLE IF NOT EXISTS mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias VARCHAR(500) NOT NULL,
    matched_text VARCHAR(500) NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    rule_id VARCHAR(100) NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(entity_id, char_start, char_end, matched_text)
);
CREATE INDEX IF NOT EXISTS ix_mentions_entity ON mentions(entity_id);

CREATE TABLE IF NOT EXISTS index_df (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL REFERENCES index_generations(id) ON DELETE CASCADE,
    term VARCHAR(200) NOT NULL,
    df INTEGER NOT NULL DEFAULT 0,
    UNIQUE(generation_id, term)
);

CREATE TABLE IF NOT EXISTS doc_term_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL REFERENCES index_generations(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    term VARCHAR(200) NOT NULL,
    tf INTEGER NOT NULL DEFAULT 0,
    weight FLOAT NOT NULL DEFAULT 0.0,
    UNIQUE(generation_id, document_id, term)
);
CREATE INDEX IF NOT EXISTS ix_doc_term_stats_gen_doc
    ON doc_term_stats(generation_id, document_id);

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL REFERENCES index_generations(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    term VARCHAR(200) NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    UNIQUE(generation_id, document_id, term, char_start)
);
CREATE INDEX IF NOT EXISTS ix_postings_gen_term
    ON postings(generation_id, term);
