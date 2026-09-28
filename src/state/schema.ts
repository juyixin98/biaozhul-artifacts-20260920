/**
 * SQLite schema for the operation ledger and diagnostic trail.
 *
 * Three tables, each with a distinct responsibility:
 *
 *   rpc_requests - one row per HTTP payload. This is the only place parse
 *                  failures and empty batches can be recorded, because they
 *                  produce no per-message rows.
 *   rpc_calls    - one row per batch element / single message, including
 *                  notifications and invalid elements. rpc_id_json stores the
 *                  JSON encoding of the request id so that numeric 1, string
 *                  "1" and null never collide; notifications store NULL.
 *   operations   - one row per side-effecting invocation. op_seq is the
 *                  independent operation number (the business/audit identity);
 *                  it is deliberately NOT derived from the RPC id, which is a
 *                  transport correlation value that clients may reuse.
 */

export const SCHEMA_SQL = `
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS rpc_requests (
  request_corr        TEXT PRIMARY KEY,
  received_at         TEXT NOT NULL,
  finished_at         TEXT,
  payload_kind        TEXT NOT NULL,
  size_bytes          INTEGER NOT NULL,
  call_count          INTEGER NOT NULL DEFAULT 0,
  notification_count  INTEGER NOT NULL DEFAULT 0,
  invalid_count       INTEGER NOT NULL DEFAULT 0,
  verdict             TEXT NOT NULL,
  layer               TEXT NOT NULL,
  failure_category    TEXT,
  reason              TEXT
);

CREATE TABLE IF NOT EXISTS rpc_calls (
  call_corr         TEXT PRIMARY KEY,
  request_corr      TEXT NOT NULL REFERENCES rpc_requests(request_corr),
  position          INTEGER NOT NULL,
  rpc_id_json       TEXT,
  is_notification   INTEGER NOT NULL,
  method            TEXT,
  params_json       TEXT,
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  status            TEXT NOT NULL,
  error_code        INTEGER,
  error_category    TEXT,
  error_message     TEXT
);

CREATE TABLE IF NOT EXISTS operations (
  op_seq            INTEGER PRIMARY KEY AUTOINCREMENT,
  call_corr         TEXT NOT NULL REFERENCES rpc_calls(call_corr),
  request_corr      TEXT NOT NULL,
  rpc_id_json       TEXT,
  idempotency_key   TEXT,
  kind              TEXT NOT NULL,
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  status            TEXT NOT NULL,
  redacted_input    TEXT,
  result_json       TEXT,
  error_code        INTEGER,
  error_category    TEXT,
  error_message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_request ON rpc_calls(request_corr);
CREATE INDEX IF NOT EXISTS idx_calls_rpc_id  ON rpc_calls(rpc_id_json);
CREATE INDEX IF NOT EXISTS idx_ops_call      ON operations(call_corr);
CREATE INDEX IF NOT EXISTS idx_ops_idem      ON operations(idempotency_key);
`;
