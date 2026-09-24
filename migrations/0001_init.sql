-- 产物晋级原子性服务 schema
-- 所有晋级决策只引用不可变标识：artifact.digest、evidence.version、policy.version。

CREATE TABLE artifacts (
    digest          TEXT PRIMARY KEY,                 -- sha256 of content, hex
    size_bytes      BIGINT NOT NULL CHECK (size_bytes >= 0),
    storage_path    TEXT NOT NULL,                   -- blob store 相对路径: ab/<digest[0:2]>/<digest>
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE env_blobs (
    env             TEXT NOT NULL,
    digest          TEXT NOT NULL REFERENCES artifacts(digest),
    copied_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_digest TEXT NOT NULL,                   -- 复制完成后重新流式 sha256 的结果
    PRIMARY KEY (env, digest)
);

CREATE TABLE test_evidence (
    evidence_id     TEXT NOT NULL,
    version         BIGINT NOT NULL CHECK (version > 0),
    artifact_digest TEXT NOT NULL REFERENCES artifacts(digest),
    tests_passed    BOOLEAN NOT NULL,
    result_json     TEXT NOT NULL,                   -- 测试结果原始 JSON
    signer_key_id   TEXT NOT NULL,
    signature       TEXT NOT NULL,                   -- base64 Ed25519 over canonical(evidence)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (evidence_id, version)
);

CREATE TABLE policies (
    policy_id       TEXT NOT NULL,
    version         BIGINT NOT NULL CHECK (version > 0),
    body_json       TEXT NOT NULL,                   -- 策略内容（含 required_tests/min_signers/approval_required/retention）
    body_sha256     TEXT NOT NULL,                   -- sha256(canonical body)
    signer_key_id   TEXT NOT NULL,
    signature       TEXT NOT NULL,                   -- 签名者对 canonical body 的 Ed25519 签名
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (policy_id, version)
);

-- 浮动标签：可用于检索/展示，但晋升接口明确拒绝按标签复制
CREATE TABLE tags (
    name        TEXT PRIMARY KEY,
    digest      TEXT NOT NULL REFERENCES artifacts(digest),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    gen         BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE approvals (
    approval_id     TEXT PRIMARY KEY,
    policy_id       TEXT NOT NULL,
    policy_version  BIGINT NOT NULL,
    env             TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    evidence_id     TEXT NOT NULL,
    evidence_version BIGINT NOT NULL,
    expected_gen    BIGINT NOT NULL,
    signer_key_id   TEXT NOT NULL,
    signature       TEXT NOT NULL,                   -- 审批人对晋升元组的 Ed25519 签名
    status          TEXT NOT NULL CHECK (status IN ('valid','consumed','stale','rejected')),
    consumed_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at     TIMESTAMPTZ
);
-- 同一审批签名只能成功消费一次
CREATE UNIQUE INDEX approvals_one_consume ON approvals(approval_id)
    WHERE status = 'consumed';

CREATE TABLE env_pointers (
    env             TEXT PRIMARY KEY,
    current_digest  TEXT REFERENCES artifacts(digest),
    gen             BIGINT NOT NULL DEFAULT 0,      -- 环境代次，CAS 防并发覆盖
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE env_history (
    env             TEXT NOT NULL,
    gen             BIGINT NOT NULL,
    digest          TEXT NOT NULL REFERENCES artifacts(digest),
    change_type     TEXT NOT NULL CHECK (change_type IN ('promote','rollback','seed')),
    attempt_id      TEXT,
    policy_id       TEXT,
    policy_version  BIGINT,
    evidence_id     TEXT,
    evidence_version BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (env, gen)
);

-- 每次晋升尝试的完整证据（成功/失败一律落库，永不更新删除）
CREATE TABLE promotion_attempts (
    id                 TEXT PRIMARY KEY,             -- attempt UUID
    env                TEXT NOT NULL,
    idem_key           TEXT,                         -- 调用方幂等键（可选）
    requested_digest   TEXT NOT NULL,
    source_evidence_id TEXT,
    evidence_version   BIGINT,
    policy_id          TEXT,
    policy_version     BIGINT,
    expected_gen       BIGINT NOT NULL,
    approval_id        TEXT,
    status             TEXT NOT NULL CHECK (status IN
                         ('in_progress','awaiting_approval','committed','copy_failed',
                          'rejected','conflict','aborted_pre_commit',
                          'recovered_committed','recovered_aborted')),
    failure_stage      TEXT,
    failure_reason     TEXT,
    copy_src_path      TEXT,
    copy_dst_path      TEXT,
    copied_verified_digest TEXT,
    gen_before         BIGINT,
    gen_after          BIGINT,
    receipt_json       TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ
);
CREATE INDEX promotion_attempts_env_time ON promotion_attempts(env, created_at);
CREATE UNIQUE INDEX promotion_attempts_idem ON promotion_attempts(env, idem_key)
    WHERE idem_key IS NOT NULL;

CREATE TABLE gc_runs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    policy_id       TEXT NOT NULL,
    policy_version  BIGINT NOT NULL,
    keep_digests    TEXT[] NOT NULL,
    removed_blobs   TEXT[] NOT NULL DEFAULT '{}',
    skipped_inuse   TEXT[] NOT NULL DEFAULT '{}',
    ran_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
