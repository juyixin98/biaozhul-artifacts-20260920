-- 可撤销凭证索引：初始 schema
-- 设计要点：
--   * issuer_keys 保留密钥生效区间 [valid_from, valid_to)，轮换只追加新行并封闭旧行；
--   * credentials 绑定签发者、主体、用途、有效期、内容摘要与签名；
--   * revocation_events 为只追加日志，seq 是全局单调快照号，
--     撤销响应与验证响应使用同一个 seq 序列作为快照号。

CREATE TABLE IF NOT EXISTS issuer_keys (
    kid        TEXT PRIMARY KEY,
    issuer     TEXT NOT NULL,
    public_key BYTEA NOT NULL,          -- ed25519 公钥（32 字节）
    secret_key BYTEA NOT NULL,          -- 仅测试用途：本地签名私钥
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to   TIMESTAMPTZ,             -- NULL 表示仍然有效
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS issuer_keys_issuer_active
    ON issuer_keys (issuer) WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS credentials (
    id             TEXT PRIMARY KEY,
    issuer         TEXT NOT NULL,
    subject        TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    not_before     TIMESTAMPTZ NOT NULL,
    not_after      TIMESTAMPTZ NOT NULL,
    content_digest TEXT NOT NULL,       -- 内容 sha256 的十六进制编码
    kid            TEXT NOT NULL REFERENCES issuer_keys (kid),
    signature      BYTEA NOT NULL,      -- 对规范化载荷的 ed25519 签名
    issued_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS revocation_events (
    seq           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- 全局快照号
    credential_id TEXT NOT NULL REFERENCES credentials (id),
    reason        TEXT NOT NULL,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS revocation_events_cred_time
    ON revocation_events (credential_id, recorded_at);
