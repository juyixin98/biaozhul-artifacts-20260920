-- 渐进发布判定器 schema
BEGIN;

CREATE TABLE IF NOT EXISTS threshold_versions (
    id                  BIGSERIAL PRIMARY KEY,
    max_error_rate      DOUBLE PRECISION NOT NULL,
    max_p95_latency_ms  DOUBLE PRECISION NOT NULL,
    note                TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rollouts (
    id                         BIGSERIAL PRIMARY KEY,
    name                       TEXT NOT NULL,
    service                    TEXT NOT NULL,
    status                     TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','paused','rolled_back','completed')),
    stage_idx                  INT NOT NULL DEFAULT 0,
    generation                 BIGINT NOT NULL DEFAULT 0,
    threshold_version_id       BIGINT NOT NULL REFERENCES threshold_versions(id),
    stage_started_at           TIMESTAMPTZ NOT NULL,
    min_samples                BIGINT NOT NULL,
    observation_window_seconds BIGINT NOT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 命令幂等表：(rollout_id, idempotency_key) 唯一，重试直接返回已存响应
CREATE TABLE IF NOT EXISTS commands (
    id                  BIGSERIAL PRIMARY KEY,
    rollout_id          BIGINT NOT NULL REFERENCES rollouts(id),
    idempotency_key     TEXT NOT NULL,
    type                TEXT NOT NULL CHECK (type IN ('promote','pause','rollback')),
    expected_generation BIGINT NOT NULL,
    request_hash        TEXT NOT NULL,
    status              TEXT NOT NULL
        CHECK (status IN ('applied','rejected_conflict','rejected_state')),
    http_status         INT NOT NULL,
    response            JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rollout_id, idempotency_key)
);

-- 每次判定落一条证据记录
CREATE TABLE IF NOT EXISTS decisions (
    id                    BIGSERIAL PRIMARY KEY,
    rollout_id            BIGINT NOT NULL REFERENCES rollouts(id),
    stage_idx             INT NOT NULL,
    generation            BIGINT NOT NULL,
    verdict               TEXT NOT NULL
        CHECK (verdict IN ('promote','hold','violation','unknown')),
    reason                TEXT NOT NULL,
    window_start          TIMESTAMPTZ NOT NULL,
    window_end            TIMESTAMPTZ NOT NULL,
    window_complete       BOOLEAN NOT NULL,
    samples               BIGINT NOT NULL,
    error_rate            DOUBLE PRECISION,      -- 指标缺失时为 NULL（未知，而非健康）
    p95_latency_ms        DOUBLE PRECISION,      -- 指标缺失时为 NULL
    coverage              DOUBLE PRECISION NOT NULL,
    metrics_status        TEXT NOT NULL CHECK (metrics_status IN ('ok','partial','missing')),
    buckets               JSONB NOT NULL DEFAULT '[]',
    threshold_version_id  BIGINT NOT NULL REFERENCES threshold_versions(id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decisions_rollout ON decisions (rollout_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_commands_rollout  ON commands (rollout_id, id DESC);

COMMIT;
