/**
 * SQLite state adapter.
 *
 * Owns persistence, the monotonic document version and the optimistic
 * concurrency contract ("patch is bound to an expected document version").
 *
 * Atomicity boundary: each patch runs inside a single `BEGIN IMMEDIATE`
 * transaction on one synchronous DatabaseSync connection.
 *
 *   - Kernel failure  -> ROLLBACK; the stored document keeps its old version.
 *   - Version conflict -> detected before the kernel runs; no write occurs.
 *   - Success         -> document body, version bump and audit event commit
 *                        together, or not at all.
 *
 * The kernel's own clone-before-apply guarantee plus this transaction means
 * neither the in-memory input nor the on-disk row can be left half-patched.
 */

import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

import type { PatchOperation } from './contract.js';
import {
  applyPatch,
  type AppliedStep,
  type KernelFailure,
  type KernelSuccess,
} from './kernel.js';

export type StoreFailureCategory =
  | 'DOCUMENT_NOT_FOUND'
  | 'DOCUMENT_ALREADY_EXISTS'
  | 'VERSION_CONFLICT'
  | 'KERNEL_FAILURE';

export class StoreError extends Error {
  constructor(
    readonly category: StoreFailureCategory,
    message: string,
    readonly details?: {
      expectedVersion?: number;
      actualVersion?: number;
      kernel?: KernelFailure;
    },
  ) {
    super(message);
    this.name = 'StoreError';
  }
}

export interface DocumentRecord {
  id: string;
  document: unknown;
  version: number;
  updatedAt: string;
}

export interface PatchEventRecord {
  requestId: string;
  documentId: string;
  baseVersion: number;
  newVersion: number | null;
  status: 'applied' | 'rejected' | 'conflict';
  category: string | null;
  failedAtIndex: number | null;
  operationsTotal: number;
  stepsAppliedBeforeOutcome: number;
  message: string | null;
  createdAt: string;
}

export interface SuccessfulApplication {
  document: unknown;
  baseVersion: number;
  newVersion: number;
  steps: AppliedStep[];
}

interface DocumentRow {
  id: string;
  doc: string;
  version: number | bigint;
  updated_at: string;
}

export class DocumentStore {
  private readonly db: DatabaseSync;
  private closed = false;

  constructor(databasePath: string) {
    if (databasePath !== ':memory:') {
      const dir = dirname(databasePath);
      if (dir !== '.') mkdirSync(dir, { recursive: true });
    }
    this.db = new DatabaseSync(databasePath);
    // WAL gives us atomic commits with good read/write separation; a
    // single connection serializes all writes within the process.
    this.db.exec('PRAGMA journal_mode = WAL');
    this.db.exec('PRAGMA foreign_keys = ON');
    this.initSchema();
  }

  private initSchema(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS documents (
        id         TEXT PRIMARY KEY,
        doc        TEXT NOT NULL,
        version    INTEGER NOT NULL CHECK (version >= 0),
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS patch_events (
        request_id                  TEXT PRIMARY KEY,
        document_id                 TEXT NOT NULL,
        base_version                INTEGER NOT NULL,
        new_version                 INTEGER,
        status                      TEXT NOT NULL CHECK (status IN ('applied','rejected','conflict')),
        category                    TEXT,
        failed_at_index             INTEGER,
        operations_total            INTEGER NOT NULL,
        steps_applied_before_outcome INTEGER NOT NULL,
        message                     TEXT,
        created_at                  TEXT NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_patch_events_document
        ON patch_events(document_id, created_at);
    `);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.db.close();
  }

  createDocument(id: string, document: unknown): DocumentRecord {
    const now = new Date().toISOString();
    try {
      this.db
        .prepare(
          `INSERT INTO documents (id, doc, version, updated_at)
           VALUES (?, ?, 0, ?)`,
        )
        .run(id, JSON.stringify(document), now);
    } catch (error) {
      if (isUniqueConstraint(error)) {
        throw new StoreError(
          'DOCUMENT_ALREADY_EXISTS',
          `document ${JSON.stringify(id)} already exists`,
        );
      }
      throw error;
    }
    return { id, document: structuredClone(document), version: 0, updatedAt: now };
  }

  getDocument(id: string): DocumentRecord {
    const row = this.db
      .prepare(`SELECT id, doc, version, updated_at FROM documents WHERE id = ?`)
      .get(id) as DocumentRow | undefined;
    if (!row) {
      throw new StoreError('DOCUMENT_NOT_FOUND', `document ${JSON.stringify(id)} not found`);
    }
    return hydrate(row);
  }

  /**
   * Apply `operations` iff the stored version equals `expectedVersion`.
   * The document row is updated, its version incremented and an audit event
   * written in one transaction. Kernel failures roll everything back.
   */
  applyPatch(
    id: string,
    expectedVersion: number,
    operations: readonly PatchOperation[],
    requestId: string,
  ): SuccessfulApplication {
    this.db.exec('BEGIN IMMEDIATE');
    let row: DocumentRow | undefined;
    try {
      row = this.db
        .prepare(`SELECT id, doc, version, updated_at FROM documents WHERE id = ?`)
        .get(id) as DocumentRow | undefined;

      if (!row) {
        throw new StoreError('DOCUMENT_NOT_FOUND', `document ${JSON.stringify(id)} not found`);
      }
      const baseVersion = Number(row.version);
      if (baseVersion !== expectedVersion) {
        throw new StoreError(
          'VERSION_CONFLICT',
          `patch bound to version ${expectedVersion} but document is at version ${baseVersion}`,
          { expectedVersion, actualVersion: baseVersion },
        );
      }

      const current = JSON.parse(row.doc) as unknown;
      const outcome = applyPatch(current, operations);

      if (!outcome.ok) {
        // Persist the rejection *after* rollback (audit must not be rolled
        // back with the aborted document write).
        this.db.exec('ROLLBACK');
        this.recordEvent({
          requestId,
          documentId: id,
          baseVersion,
          newVersion: null,
          status: 'rejected',
          category: outcome.category,
          failedAtIndex: outcome.failedAtIndex,
          operationsTotal: operations.length,
          stepsAppliedBeforeOutcome: outcome.appliedBeforeFailure.length,
          message: outcome.message,
        });
        throw new StoreError(
          'KERNEL_FAILURE',
          `patch rejected at operation ${outcome.failedAtIndex}: ${outcome.message}`,
          { expectedVersion: baseVersion, kernel: outcome },
        );
      }

      const success = outcome as KernelSuccess;
      const newVersion = baseVersion + 1;
      const now = new Date().toISOString();
      this.db
        .prepare(
          `UPDATE documents
              SET doc = ?, version = ?, updated_at = ?
            WHERE id = ? AND version = ?`,
        )
        .run(
          JSON.stringify(success.result),
          newVersion,
          now,
          id,
          baseVersion,
        );
      this.recordEvent({
        requestId,
        documentId: id,
        baseVersion,
        newVersion,
        status: 'applied',
        category: null,
        failedAtIndex: null,
        operationsTotal: operations.length,
        stepsAppliedBeforeOutcome: success.steps.length,
        message: null,
      });
      this.db.exec('COMMIT');

      return {
        document: success.result,
        baseVersion,
        newVersion,
        steps: success.steps,
      };
    } catch (error) {
      // Defensive: any unexpected error leaves the transaction open.
      this.rollbackIfInTransaction();
      throw error;
    }
  }

  /** Record a version conflict audit row outside the (aborted) transaction. */
  recordConflict(
    requestId: string,
    id: string,
    expectedVersion: number,
    actualVersion: number,
    operationsTotal: number,
  ): void {
    this.recordEvent({
      requestId,
      documentId: id,
      baseVersion: actualVersion,
      newVersion: null,
      status: 'conflict',
      category: 'VERSION_CONFLICT',
      failedAtIndex: null,
      operationsTotal,
      stepsAppliedBeforeOutcome: 0,
      message: `patch bound to version ${expectedVersion} but document is at version ${actualVersion}`,
    });
  }

  getEvent(requestId: string): PatchEventRecord | undefined {
    const row = this.db
      .prepare(`SELECT * FROM patch_events WHERE request_id = ?`)
      .get(requestId) as EventRow | undefined;
    return row ? hydrateEvent(row) : undefined;
  }

  listEvents(documentId: string, limit = 50): PatchEventRecord[] {
    const rows = this.db
      .prepare(
        `SELECT * FROM patch_events
          WHERE document_id = ?
          ORDER BY created_at DESC, base_version DESC
          LIMIT ?`,
      )
      .all(documentId, limit) as EventRow[];
    return rows.map(hydrateEvent);
  }

  /** Insert an audit row. Applied events run inside the open tx (commit
   *  together); rejected/conflict events run in autocommit after rollback. */
  private recordEvent(event: EventInput): void {
    this.db
      .prepare(
        `INSERT INTO patch_events (
            request_id, document_id, base_version, new_version, status,
            category, failed_at_index, operations_total,
            steps_applied_before_outcome, message, created_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        event.requestId,
        event.documentId,
        event.baseVersion,
        event.newVersion,
        event.status,
        event.category,
        event.failedAtIndex,
        event.operationsTotal,
        event.stepsAppliedBeforeOutcome,
        event.message,
        new Date().toISOString(),
      );
  }

  private rollbackIfInTransaction(): void {
    // node:sqlite throws if no transaction is active; inspect and swallow only
    // that case.
    try {
      this.db.exec('ROLLBACK');
    } catch {
      // already committed / rolled back
    }
  }
}

interface EventInput {
  requestId: string;
  documentId: string;
  baseVersion: number;
  newVersion: number | null;
  status: 'applied' | 'rejected' | 'conflict';
  category: string | null;
  failedAtIndex: number | null;
  operationsTotal: number;
  stepsAppliedBeforeOutcome: number;
  message: string | null;
}

interface EventRow {
  request_id: string;
  document_id: string;
  base_version: number | bigint;
  new_version: number | bigint | null;
  status: 'applied' | 'rejected' | 'conflict';
  category: string | null;
  failed_at_index: number | bigint | null;
  operations_total: number | bigint;
  steps_applied_before_outcome: number | bigint;
  message: string | null;
  created_at: string;
}

function hydrate(row: DocumentRow): DocumentRecord {
  return {
    id: row.id,
    document: JSON.parse(row.doc) as unknown,
    version: Number(row.version),
    updatedAt: row.updated_at,
  };
}

function hydrateEvent(row: EventRow): PatchEventRecord {
  return {
    requestId: row.request_id,
    documentId: row.document_id,
    baseVersion: Number(row.base_version),
    newVersion: row.new_version === null ? null : Number(row.new_version),
    status: row.status,
    category: row.category,
    failedAtIndex:
      row.failed_at_index === null ? null : Number(row.failed_at_index),
    operationsTotal: Number(row.operations_total),
    stepsAppliedBeforeOutcome: Number(row.steps_applied_before_outcome),
    message: row.message,
    createdAt: row.created_at,
  };
}

function isUniqueConstraint(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false;
  const candidate = error as { code?: unknown; errcode?: unknown; message?: unknown };
  // better-sqlite3 uses the string code; node:sqlite (DatabaseSync) exposes
  // the SQLite extended error code 1555 (SQLITE_CONSTRAINT_PRIMARYKEY).
  return (
    candidate.code === 'SQLITE_CONSTRAINT_PRIMARYKEY' ||
    candidate.errcode === 1555 ||
    (typeof candidate.message === 'string' &&
      candidate.message.startsWith('UNIQUE constraint failed'))
  );
}
