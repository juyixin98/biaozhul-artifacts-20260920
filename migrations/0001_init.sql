-- 通道：每通道维护连续 nonce；失败时 blocked=true，该通道后续消息全部暂停。
CREATE TABLE IF NOT EXISTS channels (
    channel_id     TEXT PRIMARY KEY,
    next_nonce     BIGINT       NOT NULL DEFAULT 0,
    blocked        BOOLEAN      NOT NULL DEFAULT FALSE,
    blocked_reason TEXT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 全局单调递增的 fencing token 计数器。每次领取租约 +1，
-- 旧代中继携带的旧 token 回执会被拒绝，不能覆盖新代状态。
CREATE TABLE IF NOT EXISTS fencing_seq (
    id    INT PRIMARY KEY CHECK (id = 1),
    value BIGINT NOT NULL DEFAULT 0
);
INSERT INTO fencing_seq (id, value) VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS messages (
    submission_id    UUID PRIMARY KEY,
    channel_id       TEXT        NOT NULL REFERENCES channels (channel_id),
    nonce            BIGINT      NOT NULL,
    payload          JSONB       NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'pending',
    -- pending | leased | confirmed | failed
    fencing_token    BIGINT      NOT NULL DEFAULT 0,
    lease_owner      TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempts         INTEGER     NOT NULL DEFAULT 0,
    target_tx_id     TEXT,
    result           JSONB,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (channel_id, nonce)
);

-- 协调器按"队头"领取，只找 pending/lease 已过期的消息。
CREATE INDEX IF NOT EXISTS messages_channel_head_idx
    ON messages (channel_id, nonce)
    WHERE status IN ('pending', 'leased');
