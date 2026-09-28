/**
 * SQLite persistence adapter (state adaptation layer).
 *
 * Visibility rule: nothing is written while bytes are still arriving. The
 * service calls {@link SubmissionRepository.commitAsync} exactly once, AFTER
 * the parser verified the closing boundary. commitAsync runs in a single
 * deferred transaction — either every row is durable or none are, so a
 * failure halfway through never leaves a half-visible submission.
 *
 * File part bytes are stored in a `part_blobs` side table so listing and
 * diagnostics queries never haul BLOBs; metadata lives in `parts`.
 */

import Database, { type Database as DB } from 'better-sqlite3';
import { computeError, isMultipartError } from '../protocol/errors.js';
import type {
  CommittedSubmission,
  CompletedPart,
  ParsedForm,
} from '../protocol/types.js';

export interface StoredPart {
  id: number;
  submission_id: number;
  name: string;
  kind: 'field' | 'file';
  filename: string | null;
  content_type: string;
  size: number;
  sha256: string;
  value: string | null;
}

export interface StoredSubmission {
  id: number;
  created_at: string;
  field_count: number;
  file_count: number;
  total_size: number;
  parts: StoredPart[];
}

const SCHEMA = `
CREATE TABLE IF NOT EXISTS submissions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at   TEXT    NOT NULL,
  field_count  INTEGER NOT NULL,
  file_count   INTEGER NOT NULL,
  total_size   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS parts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
  name          TEXT    NOT NULL,
  kind          TEXT    NOT NULL CHECK (kind IN ('field','file')),
  filename      TEXT,
  content_type  TEXT    NOT NULL,
  size          INTEGER NOT NULL,
  sha256        TEXT    NOT NULL,
  value         TEXT
);
CREATE TABLE IF NOT EXISTS part_blobs (
  part_id INTEGER PRIMARY KEY REFERENCES parts(id) ON DELETE CASCADE,
  data    BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parts_submission ON parts(submission_id);
`;

interface SubmissionRow {
  id: number;
  created_at: string;
  field_count: number;
  file_count: number;
  total_size: number;
}

interface ResolvedPart {
  part: CompletedPart;
  blob: Buffer | null;
}

export class SubmissionRepository {
  private readonly db: DB;

  constructor(dbPathOrConnection: string | DB) {
    if (typeof dbPathOrConnection === 'string') {
      this.db = new Database(dbPathOrConnection);
    } else {
      this.db = dbPathOrConnection;
    }
    this.db.pragma('journal_mode = WAL');
    this.db.pragma('foreign_keys = ON');
    this.db.exec(SCHEMA);
  }

  close(): void {
    this.db.close();
  }

  /**
   * Atomically persist a fully parsed form.
   *
   * Temp files are read BEFORE the transaction begins (better-sqlite3
   * transactions are synchronous, so no async I/O may happen inside one).
   * Every row — submission, part metadata, and file BLOB — shares one
   * transaction; a failure rolls all of it back.
   */
  async commitAsync(
    form: ParsedForm,
    resolveBlob: (part: CompletedPart) => Promise<Buffer>,
  ): Promise<CommittedSubmission> {
    try {
      return await this.commitInner(form, resolveBlob);
    } catch (err) {
      if (isMultipartError(err) && err.errorClass === 'COMPUTE_ERROR') throw err;
      throw computeError('DB_ERROR', 'commit failed; submission was not stored', {
        reason: err instanceof Error ? err.message : String(err),
      });
    }
  }

  private async commitInner(
    form: ParsedForm,
    resolveBlob: (part: CompletedPart) => Promise<Buffer>,
  ): Promise<CommittedSubmission> {
    const resolved: ResolvedPart[] = await Promise.all(
      form.parts.map(async (part): Promise<ResolvedPart> => {
        if (part.meta.kind !== 'file') return { part, blob: null };
        const blob = await resolveBlob(part);
        if (blob.length !== part.size) {
          throw computeError('DB_ERROR', 'spooled file size disagrees with parsed size', {
            field: part.meta.name,
            parsed: part.size,
            spooled: blob.length,
          });
        }
        return { part, blob };
      }),
    );

    const insertSubmission = this.db.prepare(
      `INSERT INTO submissions (created_at, field_count, file_count, total_size)
       VALUES (?, ?, ?, ?)`,
    );
    const insertPart = this.db.prepare(
      `INSERT INTO parts
         (submission_id, name, kind, filename, content_type, size, sha256, value)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    );
    const insertBlob = this.db.prepare(
      'INSERT INTO part_blobs (part_id, data) VALUES (?, ?)',
    );

    const fieldCount = form.parts.filter((p) => p.meta.kind === 'field').length;
    const fileCount = form.parts.length - fieldCount;

    try {
      const submissionId = this.db.transaction(() => {
        const result = insertSubmission.run(
          new Date().toISOString(),
          fieldCount,
          fileCount,
          form.totalSize,
        );
        const id = Number(result.lastInsertRowid);
        for (const { part, blob } of resolved) {
          const partResult = insertPart.run(
            id,
            part.meta.name,
            part.meta.kind,
            part.meta.filename ?? null,
            part.meta.contentType,
            part.size,
            part.sha256,
            part.meta.kind === 'field' ? part.value ?? null : null,
          );
          if (blob) {
            insertBlob.run(Number(partResult.lastInsertRowid), blob);
          }
        }
        return id;
      })();

      return {
        id: submissionId,
        createdAt: new Date().toISOString(),
        fieldCount,
        fileCount,
        totalSize: form.totalSize,
      };
    } catch (err) {
      if (isMultipartError(err)) throw err;
      throw computeError('DB_ERROR', 'database transaction failed; submission was not stored', {
        reason: err instanceof Error ? err.message : String(err),
      });
    }
  }

  list(limit = 20): StoredSubmission[] {
    const rows = this.db
      .prepare('SELECT * FROM submissions ORDER BY id DESC LIMIT ?')
      .all(limit) as SubmissionRow[];
    return rows.map((row) => this.hydrate(row));
  }

  get(id: number): StoredSubmission | null {
    const row = this.db
      .prepare('SELECT * FROM submissions WHERE id = ?')
      .get(id) as SubmissionRow | undefined;
    return row ? this.hydrate(row) : null;
  }

  /** Read a stored file part's bytes (diagnostics/download). */
  getPartBlob(submissionId: number, partId: number): Buffer | null {
    const row = this.db
      .prepare(
        `SELECT id FROM parts WHERE id = ? AND submission_id = ? AND kind = 'file'`,
      )
      .get(partId, submissionId) as { id: number } | undefined;
    if (!row) return null;
    const blobRow = this.db
      .prepare('SELECT data FROM part_blobs WHERE part_id = ?')
      .get(partId) as { data: Buffer } | undefined;
    return blobRow?.data ?? null;
  }

  count(): number {
    return (this.db.prepare('SELECT COUNT(*) AS n FROM submissions').get() as { n: number }).n;
  }

  private hydrate(row: SubmissionRow): StoredSubmission {
    const parts = this.db
      .prepare('SELECT * FROM parts WHERE submission_id = ? ORDER BY id')
      .all(row.id) as StoredPart[];
    return {
      id: row.id,
      created_at: row.created_at,
      field_count: row.field_count,
      file_count: row.file_count,
      total_size: row.total_size,
      parts,
    };
  }
}
