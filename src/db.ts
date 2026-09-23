import Database from "better-sqlite3";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

const SCHEMA = `
CREATE TABLE IF NOT EXISTS blocks (
  cid        TEXT PRIMARY KEY,   -- canonical CID string
  version    INTEGER NOT NULL,   -- 0 | 1
  codec      INTEGER NOT NULL,   -- multicodec code
  hash_code  INTEGER NOT NULL,   -- always 0x12 (sha2-256)
  size       INTEGER NOT NULL,
  data       BLOB NOT NULL
);

-- Lightweight local chain: each row is a canonical block in slot order.
CREATE TABLE IF NOT EXISTS chain_blocks (
  height     INTEGER PRIMARY KEY,
  block_hash TEXT NOT NULL,      -- 0x-prefixed hex caller-supplied chain hash
  parent_hash TEXT
);

-- One token per token_id; current_cid follows the LATEST revision on the
-- canonical chain (pending or confirmed).
CREATE TABLE IF NOT EXISTS tokens (
  token_id    TEXT PRIMARY KEY,
  current_cid TEXT,
  created_at  INTEGER NOT NULL
);

-- Every metadata revision ever registered. Old CIDs and their anchor blocks
-- are retained forever (revoked/superseded rows are never deleted).
CREATE TABLE IF NOT EXISTS revisions (
  token_id   TEXT NOT NULL,
  version    INTEGER NOT NULL,
  cid        TEXT NOT NULL,
  anchor_height INTEGER NOT NULL,      -- chain height at which the update landed
  anchor_hash   TEXT NOT NULL,
  status     TEXT NOT NULL,            -- pending|active|superseded|revoked
  evidence   TEXT NOT NULL,            -- JSON verification evidence snapshot
  created_at INTEGER NOT NULL,
  PRIMARY KEY (token_id, version),
  UNIQUE (token_id, cid)
);

CREATE INDEX IF NOT EXISTS idx_revisions_token ON revisions(token_id, version);
`;

export function openDatabase(path: string): Database.Database {
  if (path !== ":memory:") {
    mkdirSync(dirname(path), { recursive: true });
  }
  const db = new Database(path);
  db.pragma("journal_mode = WAL");
  db.pragma("foreign_keys = ON");
  db.exec(SCHEMA);
  return db;
}
