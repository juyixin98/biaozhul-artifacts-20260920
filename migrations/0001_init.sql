CREATE TABLE IF NOT EXISTS conversion_audit (
  id                BIGSERIAL PRIMARY KEY,
  record_id         TEXT        NOT NULL,
  direction         TEXT        NOT NULL CHECK (direction IN ('v1->v2','v2->v1')),
  source_version    TEXT        NOT NULL,
  target_version    TEXT        NOT NULL,
  converter_version TEXT        NOT NULL,
  mapping_version   TEXT        NOT NULL,
  status            TEXT        NOT NULL CHECK (status IN ('ok','error')),
  error_detail      JSONB,
  payload_sha256    TEXT        NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversion_audit_record_id_idx ON conversion_audit (record_id);
CREATE INDEX IF NOT EXISTS conversion_audit_created_at_idx ON conversion_audit (created_at);
