-- 权益惩罚证据归并后端 schema
-- 所有金额/权重均为整数（微单位，1_000_000 = 1 个代币），全程整数运算。

-- 链：每条链有自己的 epoch 长度；chain_id 是跨链隔离的边界。
CREATE TABLE IF NOT EXISTS chains (
    chain_id      TEXT PRIMARY KEY,
    epoch_length  BIGINT NOT NULL CHECK (epoch_length > 0),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 验证者注册表：公钥只在注册的链上有效。
-- 主键 (chain_id, validator_pubkey) 天然阻止「同一公钥跨链复用」的签名被接受：
-- 链 A 的投票在链 B 的注册表里查不到注册记录。
CREATE TABLE IF NOT EXISTS validators (
    chain_id          TEXT NOT NULL REFERENCES chains(chain_id),
    validator_pubkey  BYTEA NOT NULL,
    moniker           TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, validator_pubkey)
);

-- 当前（可变）权益表：后续委托只修改这里，不影响已冻结的 epoch 快照。
CREATE TABLE IF NOT EXISTS validator_power (
    chain_id          TEXT NOT NULL,
    validator_pubkey  BYTEA NOT NULL,
    power             BIGINT NOT NULL CHECK (power >= 0),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, validator_pubkey),
    FOREIGN KEY (chain_id, validator_pubkey)
        REFERENCES validators(chain_id, validator_pubkey)
);

-- epoch 边界冻结的权益快照（不可变）。
CREATE TABLE IF NOT EXISTS stake_snapshots (
    chain_id      TEXT NOT NULL REFERENCES chains(chain_id),
    epoch         BIGINT NOT NULL CHECK (epoch >= 0),
    frozen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    snapshot_hash BYTEA NOT NULL,           -- 快照内容的规范哈希
    total_power   BIGINT NOT NULL,
    PRIMARY KEY (chain_id, epoch)
);

-- 快照中的验证者权益明细（不可变）。惩罚引用的是这里的历史基数。
CREATE TABLE IF NOT EXISTS stake_snapshot_entries (
    chain_id          TEXT NOT NULL,
    epoch             BIGINT NOT NULL,
    validator_pubkey  BYTEA NOT NULL,
    power             BIGINT NOT NULL CHECK (power >= 0),
    PRIMARY KEY (chain_id, epoch, validator_pubkey),
    FOREIGN KEY (chain_id, epoch)
        REFERENCES stake_snapshots(chain_id, epoch)
);

-- 双签证据。冲突键保证「同一 (链, 验证者, 轮次) 只归并出一份证据，只处罚一次」。
-- 迟到的更多冲突票写入 votes 但不会再产生证据或惩罚。
CREATE TABLE IF NOT EXISTS evidences (
    evidence_id      BYTEA PRIMARY KEY,          -- 规范排序后哈希出的唯一 ID
    chain_id         TEXT NOT NULL,
    validator_pubkey BYTEA NOT NULL,
    round            BIGINT NOT NULL CHECK (round >= 0),
    epoch            BIGINT NOT NULL,
    -- 证据原文：两份冲突票的完整规范载荷（JSONB 原样保存）。
    raw_evidence     JSONB NOT NULL,
    -- 规范化后的证据（排序后，证据 ID 即对此字节做 SHA-256）。
    canonical_blob   BYTEA NOT NULL,
    judge_version    TEXT NOT NULL,              -- 判定版本
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'punished')),
    UNIQUE (chain_id, validator_pubkey, round)
);

-- 收到的每一张离线投票原文。
CREATE TABLE IF NOT EXISTS votes (
    chain_id          TEXT NOT NULL,
    validator_pubkey  BYTEA NOT NULL,
    round             BIGINT NOT NULL CHECK (round >= 0),
    block_hash        BYTEA NOT NULL,
    signature         BYTEA NOT NULL,
    -- 投票内容哈希：对「签名所覆盖的规范字节」求 SHA-256。
    -- 同一验证者重复提交相同投票 -> content_hash 相同 -> 去重（不算双签）。
    content_hash      BYTEA NOT NULL,
    raw_vote          JSONB NOT NULL,           -- 投票原文（原样保存）
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 若该票参与了某份双签证据，回填证据 ID。
    evidence_id       BYTEA REFERENCES evidences(evidence_id),
    PRIMARY KEY (chain_id, signature)
);

-- 同一 (链, 验证者, 轮次) 下相同内容只保留一张：拒绝重复输入。
CREATE UNIQUE INDEX IF NOT EXISTS votes_unique_content
    ON votes (chain_id, validator_pubkey, round, content_hash);

-- 惩罚：与证据一一对应，幂等应用，崩溃后可安全重试。
CREATE TABLE IF NOT EXISTS penalties (
    evidence_id       BYTEA PRIMARY KEY
                      REFERENCES evidences(evidence_id),
    chain_id          TEXT NOT NULL,
    epoch             BIGINT NOT NULL,            -- 处罚对应的快照 epoch
    validator_pubkey  BYTEA NOT NULL,
    -- 引用的快照行 (chain_id, epoch)；后续委托改不了这个基数。
    snapshot_chain_id TEXT NOT NULL,
    snapshot_epoch    BIGINT NOT NULL,
    base_power        BIGINT NOT NULL,            -- 快照中的权益基数
    slashed_power     BIGINT NOT NULL,            -- 实际扣减（整数除法）
    judge_version     TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'applied'
                      CHECK (status IN ('applied', 'deferred')),
    applied_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (snapshot_chain_id, snapshot_epoch)
        REFERENCES stake_snapshots(chain_id, epoch)
);
