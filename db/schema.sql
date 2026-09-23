-- MQTT 会话重投递演示:遥测接收后端 schema
-- 业务事件键 = (device_id, boot_gen, seq)

BEGIN;

CREATE TABLE IF NOT EXISTS raw_messages (
    id          BIGSERIAL PRIMARY KEY,
    topic       TEXT        NOT NULL,
    payload     BYTEA       NOT NULL,
    qos         SMALLINT    NOT NULL,
    retained    BOOLEAN     NOT NULL,
    dup         BOOLEAN     NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS samples (
    id              BIGSERIAL PRIMARY KEY,
    device_id       TEXT        NOT NULL,
    boot_gen        BIGINT      NOT NULL,
    seq             BIGINT      NOT NULL,
    temperature     DOUBLE PRECISION NOT NULL,
    humidity        DOUBLE PRECISION NOT NULL,
    sampled_at      TIMESTAMPTZ NOT NULL,
    raw_message_id  BIGINT      NOT NULL REFERENCES raw_messages(id),
    -- 该条是「保留消息重放」时置真:只落库,不刷新设备在线状态
    retained_replay BOOLEAN     NOT NULL DEFAULT false,
    -- 该条属于旧启动代次(设备已重启到更新的 boot_gen)时置真
    stale_generation BOOLEAN    NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 业务去重键:同一设备同一代次同一序号只提交一次
    CONSTRAINT samples_business_key UNIQUE (device_id, boot_gen, seq)
);

CREATE TABLE IF NOT EXISTS devices (
    device_id        TEXT PRIMARY KEY,
    current_boot_gen BIGINT      NOT NULL,
    last_seq         BIGINT      NOT NULL,
    last_sample_at   TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quarantine (
    id             BIGSERIAL PRIMARY KEY,
    raw_message_id BIGINT      NOT NULL REFERENCES raw_messages(id),
    reason         TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
