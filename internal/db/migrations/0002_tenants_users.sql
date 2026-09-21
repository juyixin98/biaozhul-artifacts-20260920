CREATE TABLE merchants (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name           TEXT NOT NULL CHECK (length(btrim(name)) > 0),
    fee_bps        INTEGER NOT NULL DEFAULT 290 CHECK (fee_bps >= 0 AND fee_bps <= 10000),
    fee_fixed      BIGINT NOT NULL DEFAULT 30  CHECK (fee_fixed >= 0),
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
    api_key_hash   TEXT,                            -- hash of the operator API key
    api_key_prefix TEXT,                            -- short masked identifier, e.g. "cs_live_ab12"
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_merchants_updated BEFORE UPDATE ON merchants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin','operator','auditor')),
    merchant_id   UUID REFERENCES merchants(id),
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (role = 'admin' OR merchant_id IS NOT NULL),
    CHECK (role <> 'admin' OR merchant_id IS NULL)
);

CREATE TRIGGER trg_users_updated BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_users_merchant ON users(merchant_id);
