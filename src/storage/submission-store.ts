/**
 * SQLite persistence behind a commit gate.
 *
 * Nothing is visible to readers until `commitSubmission` succeeds, and that is
 * called only after the parser validated the terminating boundary. Any earlier
 * failure rolls the transaction back and the request's temp directory is wiped
 * by the upload session, so partial uploads can never become queryable rows.
 */

import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { ErrorCode, MultipartError } from '../protocol/errors.js';
import type { ParseCounters } from '../protocol/multipart-parser.js';

export interface StoredPartRow {
  partIndex: number;
  name: string;
  isFile: boolean;
  filename: string | null;
  storedName: string | null;
  contentType: string | null;
  size: number;
  sha256: string;
  /** field value for non-file parts, always null for files */
  value: string | null;
}

export interface SubmissionRecord {
  id: string;
  createdAt: string;
  totalBodyBytes: number;
  headerBytes: number;
  partsCount: number;
  fieldsCount: number;
  filesCount: number;
  parts: StoredPartRow[];
}

export class SubmissionStore {
  private readonly db: DatabaseSync;

  constructor(dbPath: string) {
    mkdirSync(dirname(dbPath), { recursive: true });
    try {
      this.db = new DatabaseSync(dbPath);
      this.db.exec('PRAGMA journal_mode = WAL');
      this.db.exec('PRAGMA foreign_keys = ON');
      this.db.exec(`
        CREATE TABLE IF NOT EXISTS submissions (
          id               TEXT PRIMARY KEY,
          created_at       TEXT NOT NULL,
          total_body_bytes INTEGER NOT NULL,
          header_bytes     INTEGER NOT NULL,
          parts_count      INTEGER NOT NULL,
          fields_count     INTEGER NOT NULL,
          files_count      INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS submission_parts (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
          part_index   INTEGER NOT NULL,
          name         TEXT NOT NULL,
          is_file      INTEGER NOT NULL,
          filename     TEXT,
          stored_name  TEXT,
          content_type TEXT,
          size         INTEGER NOT NULL,
          sha256       TEXT NOT NULL,
          value        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_parts_submission ON submission_parts(submission_id);
      `);
    } catch (err) {
      throw new MultipartError(
        ErrorCode.SQLITE_FAILURE,
        `failed to open/initialize database at ${dbPath}: ${(err as Error).message}`,
        { dbPath }
      );
    }
  }

  close(): void {
    try {
      this.db.close();
    } catch {
      // closing during shutdown; ignore
    }
  }

  /**
   * Atomically publish one submission. Runs inside an IMMEDIATE transaction:
   * readers never see a half-inserted submission.
   */
  commitSubmission(id: string, counters: ParseCounters, parts: StoredPartRow[]): SubmissionRecord {
    const insertSubmission = this.db.prepare(`
      INSERT INTO submissions (id, created_at, total_body_bytes, header_bytes, parts_count, fields_count, files_count)
      VALUES (?, ?, ?, ?, ?, ?, ?)
    `);
    const insertPart = this.db.prepare(`
      INSERT INTO submission_parts
        (submission_id, part_index, name, is_file, filename, stored_name, content_type, size, sha256, value)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);
    const createdAt = new Date().toISOString();
    try {
      this.db.exec('BEGIN IMMEDIATE');
      insertSubmission.run(
        id,
        createdAt,
        counters.bodyBytes,
        counters.headerBytes,
        counters.parts,
        counters.fields,
        counters.files
      );
      for (const p of parts) {
        insertPart.run(
          id,
          p.partIndex,
          p.name,
          p.isFile ? 1 : 0,
          p.filename,
          p.storedName,
          p.contentType,
          p.size,
          p.sha256,
          p.value
        );
      }
      this.db.exec('COMMIT');
    } catch (err) {
      try {
        this.db.exec('ROLLBACK');
      } catch {
        // transaction may already be rolled back
      }
      throw new MultipartError(
        ErrorCode.SQLITE_FAILURE,
        `failed to commit submission ${id}: ${(err as Error).message}`,
        { submissionId: id }
      );
    }
    return {
      id,
      createdAt,
      totalBodyBytes: counters.bodyBytes,
      headerBytes: counters.headerBytes,
      partsCount: counters.parts,
      fieldsCount: counters.fields,
      filesCount: counters.files,
      parts
    };
  }

  getSubmission(id: string): SubmissionRecord | null {
    try {
      const row = this.db.prepare('SELECT * FROM submissions WHERE id = ?').get(id) as
        | Record<string, unknown>
        | undefined;
      if (!row) return null;
      const partRows = this.db
        .prepare('SELECT * FROM submission_parts WHERE submission_id = ? ORDER BY part_index')
        .all(id) as Record<string, unknown>[];
      return {
        id: row.id as string,
        createdAt: row.created_at as string,
        totalBodyBytes: row.total_body_bytes as number,
        headerBytes: row.header_bytes as number,
        partsCount: row.parts_count as number,
        fieldsCount: row.fields_count as number,
        filesCount: row.files_count as number,
        parts: partRows.map((r) => ({
          partIndex: r.part_index as number,
          name: r.name as string,
          isFile: Boolean(r.is_file),
          filename: (r.filename as string | null) ?? null,
          storedName: (r.stored_name as string | null) ?? null,
          contentType: (r.content_type as string | null) ?? null,
          size: r.size as number,
          sha256: r.sha256 as string,
          value: (r.value as string | null) ?? null
        }))
      };
    } catch (err) {
      throw new MultipartError(
        ErrorCode.SQLITE_FAILURE,
        `failed to read submission ${id}: ${(err as Error).message}`,
        { submissionId: id }
      );
    }
  }

  listRecent(limit: number): Array<{ id: string; createdAt: string; partsCount: number; filesCount: number }> {
    try {
      const rows = this.db
        .prepare('SELECT id, created_at, parts_count, files_count FROM submissions ORDER BY created_at DESC LIMIT ?')
        .all(limit) as Record<string, unknown>[];
      return rows.map((r) => ({
        id: r.id as string,
        createdAt: r.created_at as string,
        partsCount: r.parts_count as number,
        filesCount: r.files_count as number
      }));
    } catch (err) {
      throw new MultipartError(
        ErrorCode.SQLITE_FAILURE,
        `failed to list submissions: ${(err as Error).message}`,
        {}
      );
    }
  }

  count(): { submissions: number; parts: number; files: number } {
    try {
      const q = (sql: string): number =>
        (this.db.prepare(sql).get() as { n: number }).n;
      return {
        submissions: q('SELECT COUNT(*) AS n FROM submissions'),
        parts: q('SELECT COUNT(*) AS n FROM submission_parts'),
        files: q('SELECT COUNT(*) AS n FROM submission_parts WHERE is_file = 1')
      };
    } catch (err) {
      throw new MultipartError(
        ErrorCode.SQLITE_FAILURE,
        `failed to count submissions: ${(err as Error).message}`,
        {}
      );
    }
  }
}
