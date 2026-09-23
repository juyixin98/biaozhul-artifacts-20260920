-- 配额服务持久化结构（SQLite）。

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id    TEXT PRIMARY KEY,
    byte_quota   INTEGER NOT NULL,
    object_quota INTEGER NOT NULL,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES tenants(tenant_id),
    byte_size    INTEGER NOT NULL,
    object_count INTEGER NOT NULL,
    -- reserved | committed | cancelled | expired
    status       TEXT NOT NULL,
    created_at   INTEGER NOT NULL,  -- 毫秒时间戳（注入时钟）
    expires_at   INTEGER NOT NULL,  -- 预留到期时刻
    updated_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_tenant_status
    ON reservations (tenant_id, status);
