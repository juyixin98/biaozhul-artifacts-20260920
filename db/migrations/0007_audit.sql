-- Append-only audit trail for privileged and security-relevant actions.
CREATE TABLE audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    actor_user   UUID REFERENCES users(id),
    actor_role   TEXT NOT NULL,
    merchant_id  UUID REFERENCES merchants(id),
    action       TEXT NOT NULL,                -- e.g. merchant.create, payment.refund, login
    target_type  TEXT,
    target_id    TEXT,
    masked       BOOLEAN NOT NULL DEFAULT false,
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb, -- secrets must be masked before insert
    ip           TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_merchant_time ON audit_logs(merchant_id, created_at DESC);
CREATE INDEX idx_audit_actor_time ON audit_logs(actor_user, created_at DESC);
CREATE INDEX idx_audit_action_time ON audit_logs(action, created_at DESC);
