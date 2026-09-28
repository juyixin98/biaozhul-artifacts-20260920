/**
 * SQLite state adapter.
 *
 * Responsibilities (and only these):
 * - persist documents and their monotonic version;
 * - run a version-checked, atomic update inside BEGIN IMMEDIATE so the patch
 *   binds to exactly the document version the caller expected;
 * - persist an audit trail (one row per patch request, full per-step traces)
 *   independently of document commit, so failed/rolled-back attempts remain
 *   explainable without ever having mutated the document.
 *
 * The adapter contains NO patch logic: the kernel produces the new document;
 * this module only stores it.
 */

import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import type Database from 'better-sqlite3';
import type { JsonValue } from './equality';
import { PatchError } from './errors';

export interface DocumentRow {
  readonly id: string;
  readonly doc: JsonValue;
  readonly version: number;
}

export interface AuditRecord {
  readonly requestId: string;
  readonly docId: string;
  readonly expectedVersion: number | null;
  readonly ok: boolean;
  readonly category: string | null;
  readonly failedAtIndex: number | null;
  readonly message: string | null;
  readonly details: Record<string, unknown>;
  readonly fromVersion: number | null;
  readonly toVersion: number | null;
  readonly applied: number | null;
  readonly traces: unknown[];
  readonly createdAt: string;
}

interface RawAuditRow {
  request_id: string;
  doc_id: string;
  expected_version: number | null;
  ok: number;
  category: string | null;
  failed_at_index: number | null;
  message: string | null;
  details_json: string;
  from_version: number | null;
  to_version: number | null;
  applied: number | null;
  traces_json: string | null;
  created_at: string;
}

export class DocumentStore {
  constructor(
    private readonly db: Database.Database,
    private readonly pretty: boolean,
  ) {}

  init(): void {
    this.db.pragma('journal_mode = WAL');
    this.db.pragma('foreign_keys = ON');
    this.db.pragma('busy_timeout = 5000');
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS documents (
        id         TEXT PRIMARY KEY,
        doc_json   TEXT NOT NULL,
        version    INTEGER NOT NULL CHECK (version >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS patch_audit (
        request_id      TEXT PRIMARY KEY,
        doc_id          TEXT NOT NULL,
        expected_version INTEGER,
        ok              INTEGER NOT NULL CHECK (ok IN (0, 1)),
        category        TEXT,
        failed_at_index INTEGER,
        message         TEXT,
        details_json    TEXT NOT NULL DEFAULT '{}',
        from_version    INTEGER,
        to_version      INTEGER,
        applied         INTEGER,
        traces_json     TEXT,
        created_at      TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_patch_audit_doc ON patch_audit(doc_id, created_at);
    `);
  }

  createDocument(id: string, doc: JsonValue): DocumentRow {
    const now = new Date().toISOString();
    try {
      this.db
        .prepare(
          `INSERT INTO documents (id, doc_json, version, created_at, updated_at)
           VALUES (?, ?, 0, ?, ?)`,
        )
        .run(id, this.encode(doc), now, now);
    } catch (err) {
      if (isUniqueViolation(err)) {
        throw new PatchError('BAD_REQUEST_BODY', `Document ${JSON.stringify(id)} already exists`, null, { id });
      }
      throw err;
    }
    return { id, doc, version: 0 };
  }

  getDocument(id: string): DocumentRow | null {
    const row = this.db
      .prepare(`SELECT id, doc_json, version FROM documents WHERE id = ?`)
      .get(id) as { id: string; doc_json: string; version: number } | undefined;
    if (!row) return null;
    return { id: row.id, doc: this.decode(row.doc_json), version: row.version };
  }

  listDocuments(): Array<{ id: string; version: number }> {
    const rows = this.db.prepare(`SELECT id, version FROM documents ORDER BY id`).all() as Array<{
      id: string;
      version: number;
    }>;
    return rows.map((r) => ({ id: r.id, version: r.version }));
  }

  /**
   * Run `fn` under BEGIN IMMEDIATE. Any throw rolls back and propagates;
   * the row lock + version comparison inside provide optimistic concurrency
   * with a hard serialization guarantee against concurrent writers.
   */
  withImmediateTransaction<T>(fn: (tx: TransactionApi) => T): T {
    this.db.prepare('BEGIN IMMEDIATE').run();
    try {
      const out = fn(this.transactionApi());
      this.db.prepare('COMMIT').run();
      return out;
    } catch (err) {
      // Never let a rollback failure mask the original error.
      try {
        this.db.prepare('ROLLBACK').run();
      } catch {
        // Transaction already finalized (e.g. autocommit edge); nothing to do.
      }
      throw err;
    }
  }

  private transactionApi(): TransactionApi {
    const loadForUpdate = (id: string): DocumentRow => {
      const row = this.db
        .prepare(`SELECT id, doc_json, version FROM documents WHERE id = ?`)
        .get(id) as { id: string; doc_json: string; version: number } | undefined;
      if (!row) {
        throw new PatchError('DOCUMENT_NOT_FOUND', `Document ${JSON.stringify(id)} does not exist`, null, { id });
      }
      return { id: row.id, doc: this.decode(row.doc_json), version: row.version };
    };
    const storeUpdated = (id: string, doc: JsonValue, newVersion: number): void => {
      const now = new Date().toISOString();
      const info = this.db
        .prepare(
          `UPDATE documents SET doc_json = ?, version = ?, updated_at = ? WHERE id = ? AND version = ?`,
        )
        .run(this.encode(doc), newVersion, now, id, newVersion - 1);
      // Invariant: inside BEGIN IMMEDIATE the row is locked and was just read
      // at newVersion-1, so exactly one row must change.
      if (info.changes !== 1) {
        throw new PatchError('VERSION_CONFLICT', 'Optimistic update guard matched no row', null, {
          id,
          expectedBaseVersion: newVersion - 1,
        });
      }
    };
    return { loadForUpdate, storeUpdated };
  }

  /**
   * Append an audit record in its OWN transaction. It runs after the document
   * transaction has ended either way, so audit durability never couples a
   * failed patch's diagnostics to document mutation.
   */
  recordAudit(record: AuditInsert): void {
    const now = new Date().toISOString();
    this.db
      .prepare(
        `INSERT INTO patch_audit
           (request_id, doc_id, expected_version, ok, category, failed_at_index,
            message, details_json, from_version, to_version, applied, traces_json, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        record.requestId,
        record.docId,
        record.expectedVersion,
        record.ok ? 1 : 0,
        record.category,
        record.failedAtIndex,
        record.message,
        JSON.stringify(record.details ?? {}),
        record.fromVersion,
        record.toVersion,
        record.applied,
        record.traces === undefined ? null : JSON.stringify(record.traces),
        now,
      );
  }

  getAudit(requestId: string): AuditRecord | null {
    const row = this.db
      .prepare(`SELECT * FROM patch_audit WHERE request_id = ?`)
      .get(requestId) as RawAuditRow | undefined;
    return row ? this.toAudit(row) : null;
  }

  historyForDocument(docId: string, limit: number): AuditRecord[] {
    const rows = this.db
      .prepare(`SELECT * FROM patch_audit WHERE doc_id = ? ORDER BY rowid DESC LIMIT ?`)
      .all(docId, limit) as RawAuditRow[];
    return rows.map((r) => this.toAudit(r));
  }

  close(): void {
    this.db.close();
  }

  private toAudit(row: RawAuditRow): AuditRecord {
    return {
      requestId: row.request_id,
      docId: row.doc_id,
      expectedVersion: row.expected_version,
      ok: row.ok === 1,
      category: row.category,
      failedAtIndex: row.failed_at_index,
      message: row.message,
      details: JSON.parse(row.details_json) as Record<string, unknown>,
      fromVersion: row.from_version,
      toVersion: row.to_version,
      applied: row.applied,
      traces: row.traces_json === null ? [] : (JSON.parse(row.traces_json) as unknown[]),
      createdAt: row.created_at,
    };
  }

  private encode(doc: JsonValue): string {
    return this.pretty ? JSON.stringify(doc, null, 2) : JSON.stringify(doc);
  }

  private decode(text: string): JsonValue {
    return JSON.parse(text) as JsonValue;
  }
}

export interface TransactionApi {
  loadForUpdate(id: string): DocumentRow;
  storeUpdated(id: string, doc: JsonValue, newVersion: number): void;
}

export interface AuditInsert {
  readonly requestId: string;
  readonly docId: string;
  readonly expectedVersion: number | null;
  readonly ok: boolean;
  readonly category: string | null;
  readonly failedAtIndex: number | null;
  readonly message: string | null;
  readonly details: Record<string, unknown>;
  readonly fromVersion: number | null;
  readonly toVersion: number | null;
  readonly applied: number | null;
  readonly traces?: unknown;
}

function isUniqueViolation(err: unknown): boolean {
  return (
    typeof err === 'object' &&
    err !== null &&
    'code' in err &&
    (err as { code?: string }).code === 'SQLITE_CONSTRAINT_PRIMARYKEY'
  );
}

/** Open (creating parent dirs/schema) a store backed by a file or ":memory:". */
export function openStore(databasePath: string, pretty: boolean): { store: DocumentStore; db: Database.Database } {
  const BetterSqlite = require('better-sqlite3') as typeof Database;
  if (databasePath !== ':memory:') {
    mkdirSync(dirname(databasePath), { recursive: true });
  }
  const db = new BetterSqlite(databasePath);
  const store = new DocumentStore(db, pretty);
  store.init();
  return { store, db };
}
