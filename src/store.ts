/**
 * SQLite persistence layer (better-sqlite3, synchronous — fits the local
 * service and keeps block + ledger writes atomic per request).
 *
 * Tables
 * ------
 * blocks                 content-addressed raw block storage
 * tokens                 current effective metadata pointer per token
 * metadata_versions      every accepted/reorged/confirmed metadata revision
 * events                 append-only ledger: PROPOSE/CONFIRM/REORG
 */
import Database from 'better-sqlite3';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import type { CID } from './codec/cid.js';
import { cidToString } from './codec/cid.js';

export interface BlockRow {
  cid: string;
  codec: string;
  size: number;
  data: Buffer;
  stored_at: string;
}

export interface MetadataVersionRow {
  id: number;
  token_id: string;
  version: number;
  cid: string;
  status: 'proposed' | 'confirmed' | 'historical' | 'reorged';
  proposed_height: number;
  proposed_block_hash: string;
  confirmed_height: number | null;
  effective_height: number | null;
  effective_block_hash: string | null;
  superseded_at: string | null;
  created_at: string;
}

export interface EventRow {
  id: number;
  ts: string;
  type: 'PROPOSE' | 'CONFIRM' | 'REORG';
  token_id: string;
  version: number | null;
  cid: string | null;
  height: number;
  block_hash: string | null;
  detail: string;
}
export function openDb(dbPath: string): Database.Database {
  if (dbPath !== ':memory:') mkdirSync(dirname(dbPath), { recursive: true });
  const db = new Database(dbPath);
  db.pragma('journal_mode = WAL');
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 5000');
  migrate(db);
  return db;
}

function migrate(db: Database.Database): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS blocks (
      cid      TEXT PRIMARY KEY,
      codec    TEXT NOT NULL,
      size     INTEGER NOT NULL,
      data     BLOB NOT NULL,
      stored_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS tokens (
      token_id        TEXT PRIMARY KEY,
      current_version INTEGER,
      created_at      TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS metadata_versions (
      id                   INTEGER PRIMARY KEY AUTOINCREMENT,
      token_id             TEXT NOT NULL,
      version              INTEGER NOT NULL,
      proposed_height      INTEGER NOT NULL,
      proposed_block_hash  TEXT NOT NULL,
      confirmed_height     INTEGER,
      effective_height     INTEGER,
      effective_block_hash TEXT,
      superseded_at        TEXT,
      created_at           TEXT NOT NULL DEFAULT (datetime('now')),
      cid                  TEXT NOT NULL,
      status               TEXT NOT NULL,
      UNIQUE (token_id, version)
    );
    -- A (token, CID) may be proposed again after a reorg revoked the first one;
    -- at most one LIVE (non-reorged) row per (token, CID) may exist.
    CREATE UNIQUE INDEX IF NOT EXISTS uq_live_token_cid
      ON metadata_versions (token_id, cid)
      WHERE status <> 'reorged';
    CREATE INDEX IF NOT EXISTS idx_versions_status ON metadata_versions(status);

    CREATE TABLE IF NOT EXISTS events (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      ts         TEXT NOT NULL DEFAULT (datetime('now')),
      type       TEXT NOT NULL,
      token_id   TEXT NOT NULL,
      version    INTEGER,
      cid        TEXT,
      height     INTEGER NOT NULL,
      block_hash TEXT,
      detail     TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS chain_tip (
      singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
      height    INTEGER NOT NULL,
      block_hash TEXT NOT NULL
    );
  `);
}

export interface Store {
  putBlock(cid: CID, codecName: string, data: Uint8Array): boolean;
  getBlock(cid: CID): BlockRow | undefined;
  hasBlock(cid: CID): boolean;

  proposeVersion(input: {
    tokenId: string;
    cid: string;
    height: number;
    blockHash: string;
  }): MetadataVersionRow;
  hasPendingProposal(tokenId: string): MetadataVersionRow | undefined;
  getVersion(tokenId: string, version: number): MetadataVersionRow | undefined;
  getVersionByCid(tokenId: string, cid: string): MetadataVersionRow | undefined;
  listVersions(tokenId: string): MetadataVersionRow[];
  confirmVersion(id: number, effectiveHeight: number, effectiveBlockHash: string): void;
  supersedeConfirmedFor(tokenId: string): void;
  reorgProposal(id: number): void;
  setTip(height: number, blockHash: string): void;
  getTip(): { height: number; block_hash: string } | undefined;

  /** Distinct token ids that currently have an unconfirmed proposal. */
  tokensWithProposals(): string[];
  /** All versions proposed at height >= minHeight (any status). */
  versionsFromHeight(minHeight: number): MetadataVersionRow[];
  /** The currently effective (confirmed) version row of a token. */
  currentVersion(tokenId: string): MetadataVersionRow | undefined;

  logEvent(e: Omit<EventRow, 'id' | 'ts'>): void;
  listEvents(limit: number): EventRow[];
}

export function createStore(db: Database.Database): Store {
  const putBlockStmt = db.prepare(
    `INSERT OR IGNORE INTO blocks (cid, codec, size, data) VALUES (?, ?, ?, ?)`,
  );
  const getBlockStmt = db.prepare(`SELECT cid, codec, size, data, stored_at FROM blocks WHERE cid = ?`);
  const insVersion = db.prepare(
    `INSERT INTO metadata_versions
       (token_id, version, cid, status, proposed_height, proposed_block_hash)
     VALUES (@token_id, @version, @cid, 'proposed', @proposed_height, @proposed_block_hash)`,
  );
  const maxVersionStmt = db.prepare(
    `SELECT COALESCE(MAX(version), 0) AS v FROM metadata_versions WHERE token_id = ?`,
  );
  const pendingStmt = db.prepare(
    `SELECT * FROM metadata_versions WHERE token_id = ? AND status = 'proposed' ORDER BY version DESC LIMIT 1`,
  );
  const getVersionStmt = db.prepare(
    `SELECT * FROM metadata_versions WHERE token_id = ? AND version = ?`,
  );
  const getByCidStmt = db.prepare(
    `SELECT * FROM metadata_versions WHERE token_id = ? AND cid = ?
     ORDER BY CASE WHEN status = 'reorged' THEN 1 ELSE 0 END, version DESC LIMIT 1`,
  );
  const listVersionsStmt = db.prepare(
    `SELECT * FROM metadata_versions WHERE token_id = ? ORDER BY version`,
  );
  const confirmStmt = db.prepare(
    `UPDATE metadata_versions
       SET status = 'confirmed', confirmed_height = ?, effective_height = ?, effective_block_hash = ?
     WHERE id = ? AND status = 'proposed'`,
  );
  const supersedeStmt = db.prepare(
    `UPDATE metadata_versions SET status = 'historical', superseded_at = datetime('now')
     WHERE token_id = ? AND status = 'confirmed'`,
  );
  const reorgStmt = db.prepare(
    `UPDATE metadata_versions SET status = 'reorged' WHERE id = ? AND status = 'proposed'`,
  );
  const setTipStmt = db.prepare(
    `INSERT INTO chain_tip (singleton, height, block_hash) VALUES (1, ?, ?)
     ON CONFLICT(singleton) DO UPDATE SET height = excluded.height, block_hash = excluded.block_hash`,
  );
  const getTipStmt = db.prepare(`SELECT height, block_hash FROM chain_tip WHERE singleton = 1`);
  const logEventStmt = db.prepare(
    `INSERT INTO events (type, token_id, version, cid, height, block_hash, detail)
     VALUES (@type, @token_id, @version, @cid, @height, @block_hash, @detail)`,
  );
  const listEventsStmt = db.prepare(`SELECT * FROM events ORDER BY id DESC LIMIT ?`);

  return {
    putBlock(cid, codecName, data) {
      const info = putBlockStmt.run(cidToString(cid), codecName, data.length, Buffer.from(data));
      return info.changes > 0;
    },
    getBlock(cid) {
      return getBlockStmt.get(cidToString(cid)) as BlockRow | undefined;
    },
    hasBlock(cid) {
      return getBlockStmt.get(cidToString(cid)) !== undefined;
    },
    proposeVersion({ tokenId, cid, height, blockHash }) {
      const next = (maxVersionStmt.get(tokenId) as { v: number }).v + 1;
      const r = insVersion.run({
        token_id: tokenId,
        version: next,
        cid,
        proposed_height: height,
        proposed_block_hash: blockHash,
      });
      return db.prepare(`SELECT * FROM metadata_versions WHERE id = ?`).get(r.lastInsertRowid) as MetadataVersionRow;
    },
    hasPendingProposal(tokenId) {
      return pendingStmt.get(tokenId) as MetadataVersionRow | undefined;
    },
    getVersion(tokenId, version) {
      return getVersionStmt.get(tokenId, version) as MetadataVersionRow | undefined;
    },
    getVersionByCid(tokenId, cid) {
      return getByCidStmt.get(tokenId, cid) as MetadataVersionRow | undefined;
    },
    listVersions(tokenId) {
      return listVersionsStmt.all(tokenId) as MetadataVersionRow[];
    },
    confirmVersion(id, effectiveHeight, effectiveBlockHash) {
      const r = confirmStmt.run(effectiveHeight, effectiveHeight, effectiveBlockHash, id);
      if (r.changes === 0) throw new Error('confirmVersion: row was not proposed');
    },
    supersedeConfirmedFor(tokenId) {
      supersedeStmt.run(tokenId);
    },
    reorgProposal(id) {
      const r = reorgStmt.run(id);
      if (r.changes === 0) throw new Error('reorgProposal: row was not proposed');
    },
    setTip(height, blockHash) {
      setTipStmt.run(height, blockHash);
    },
    getTip() {
      return getTipStmt.get() as { height: number; block_hash: string } | undefined;
    },
    tokensWithProposals() {
      return (
        db
          .prepare(`SELECT DISTINCT token_id FROM metadata_versions WHERE status = 'proposed'`)
          .all() as { token_id: string }[]
      ).map((r) => r.token_id);
    },
    versionsFromHeight(minHeight) {
      return db
        .prepare(`SELECT * FROM metadata_versions WHERE proposed_height >= ? ORDER BY id`)
        .all(minHeight) as MetadataVersionRow[];
    },
    currentVersion(tokenId) {
      return db
        .prepare(`SELECT * FROM metadata_versions WHERE token_id = ? AND status = 'confirmed'`)
        .get(tokenId) as MetadataVersionRow | undefined;
    },
    logEvent(e) {
      logEventStmt.run(e);
    },
    listEvents(limit) {
      return listEventsStmt.all(limit) as EventRow[];
    },
  };
}
