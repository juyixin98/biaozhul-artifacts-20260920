-- 中继租约与序号协调：初始结构
-- 每个通道维护单调递增的 nonce（仅在成功领取时推进），
-- 每条提交维护租约代次（fencing token），旧代次的回执不得覆盖新代次状态。

CREATE TABLE channels (
    id           TEXT PRIMARY KEY,
    -- 下一条应分配的 nonce（严格连续，从 0 开始）
    next_nonce   BIGINT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE submissions (
    id            TEXT PRIMARY KEY,                 -- 稳定提交 ID（外部生成，绝不重发新 ID）
    channel_id    TEXT NOT NULL
                  REFERENCES channels(id),
    seq           BIGINT NOT NULL,                  -- 通道内入队顺序（0 起）
    nonce         BIGINT,                           -- 被领取时分配的通道 nonce；未领取为 NULL
    payload       JSONB NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','leased','delivered','confirmed',
                                    'failed')),
    fence         INTEGER NOT NULL DEFAULT 0,       -- 当前租约代次（每次重领 +1）
    leased_by     TEXT,                             -- 当前持有租约的中继 ID
    leased_until  TIMESTAMPTZ,                      -- 租约到期时刻
    attempts      INTEGER NOT NULL DEFAULT 0,       -- 被领取次数
    tx_hash       TEXT,                             -- 目标链回执（确认后写入）
    last_error    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (channel_id, seq)
);

CREATE INDEX idx_submissions_head
    ON submissions (channel_id, seq)
    WHERE status IN ('pending','leased','delivered');

CREATE INDEX idx_submissions_lease
    ON submissions (leased_until)
    WHERE status IN ('pending','leased','delivered');

-- 每次领取/回执/心跳留痕，便于审计与跨进程竞争排查
CREATE TABLE delivery_attempts (
    id            BIGSERIAL PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES submissions(id),
    relay_id      TEXT NOT NULL,
    fence         INTEGER NOT NULL,
    nonce         BIGINT,
    event         TEXT NOT NULL
                  CHECK (event IN ('claim','heartbeat','delivered','confirm',
                                   'retryable','fatal','requeue','stale')),
    detail        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_attempts_sub ON delivery_attempts (submission_id, id);
