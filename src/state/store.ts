import { createHash } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import type { ImmutableObject, ImmutableObjectMeta } from '../types.js';

/**
 * State adapter for immutable objects, backed by a local SQLite database.
 *
 * The adapter owns:
 *  - schema creation / migration
 *  - strong validator derivation (ETag = quoted SHA-256 of the bytes)
 *  - offset-addressable byte reads via SQLite substr() on the BLOB, so the
 *    route layer never has to load a whole object to serve one part
 *
 * Immutability contract: `put` inserts a new id; reusing an existing id is
 * rejected rather than overwriting. Every representation therefore keeps
 * one ETag and one Last-Modified for its lifetime, which is what makes
 * If-Range evaluation meaningful.
 */

export interface ObjectStore {
  put(input: PutObjectInput): ImmutableObjectMeta;
  getMeta(id: string): ImmutableObjectMeta | null;
  getObject(id: string): ImmutableObject | null;
  /** Inclusive byte slice read straight from the stored BLOB. */
  getSlice(id: string, start: number, end: number): Buffer | null;
  list(): ImmutableObjectMeta[];
  close(): void;
}

export interface PutObjectInput {
  id: string;
  data: Buffer;
  contentType: string;
  createdAt?: Date;
}

interface ObjectRow {
  id: string;
  data: Uint8Array;
  size: number;
  etag: string;
  last_modified: string;
  content_type: string;
}

export function strongEtag(data: Buffer): string {
  return `"${createHash('sha256').update(data).digest('hex')}"`;
}

function rowToMeta(row: ObjectRow): ImmutableObjectMeta {
  return {
    id: row.id,
    size: row.size,
    etag: row.etag,
    lastModified: new Date(row.last_modified),
    contentType: row.content_type,
  };
}

export class SqliteObjectStore implements ObjectStore {
  private readonly db: DatabaseSync;

  constructor(databasePath: string) {
    if (databasePath !== ':memory:') {
      mkdirSync(dirname(databasePath), { recursive: true });
    }
    this.db = new DatabaseSync(databasePath);
    this.db.exec('PRAGMA journal_mode=WAL');
    this.db.exec('PRAGMA foreign_keys=ON');
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS objects (
        id           TEXT PRIMARY KEY,
        data         BLOB NOT NULL,
        size         INTEGER NOT NULL,
        etag         TEXT NOT NULL,
        last_modified TEXT NOT NULL,
        content_type TEXT NOT NULL
      )
    `);
  }

  put({ id, data, contentType, createdAt }: PutObjectInput): ImmutableObjectMeta {
    if (!Number.isSafeInteger(data.length)) {
      throw new Error('Object exceeds safe-integer addressability');
    }
    const etag = strongEtag(data);
    const lastModified = createdAt ?? new Date();
    const stmt = this.db.prepare(
      `INSERT INTO objects (id, data, size, etag, last_modified, content_type)
       VALUES (?, ?, ?, ?, ?, ?)`,
    );
    try {
      stmt.run(
        id,
        data,
        data.length,
        etag,
        lastModified.toISOString(),
        contentType,
      );
    } catch (err) {
      if (err instanceof Error && err.message.includes('UNIQUE constraint')) {
        throw new Error(`Object "${id}" already exists; objects are immutable`);
      }
      throw err;
    }
    return { id, size: data.length, etag, lastModified, contentType };
  }

  getMeta(id: string): ImmutableObjectMeta | null {
    const row = this.db
      .prepare('SELECT id, size, etag, last_modified, content_type FROM objects WHERE id = ?')
      .get(id) as Omit<ObjectRow, 'data'> | undefined;
    return row === undefined ? null : rowToMeta(row as ObjectRow);
  }

  getObject(id: string): ImmutableObject | null {
    const row = this.db
      .prepare('SELECT id, data, size, etag, last_modified, content_type FROM objects WHERE id = ?')
      .get(id) as ObjectRow | undefined;
    if (row === undefined) return null;
    return { ...rowToMeta(row), data: Buffer.from(row.data) };
  }

  getSlice(id: string, start: number, end: number): Buffer | null {
    if (start < 0 || end < start) {
      throw new Error(`Bad slice ${start}-${end}`);
    }
    // SQLite substr() is 1-based; inclusive HTTP positions map to
    // (start+1, length = end-start+1). A missing id yields no row, while
    // a present id with a zero-length BLOB yields a row whose chunk is
    // SQL NULL — keep those cases distinct.
    const row = this.db
      .prepare('SELECT substr(data, ?1, ?2) AS chunk FROM objects WHERE id = ?3')
      .get(start + 1, end - start + 1, id) as
      | { chunk: Uint8Array | null }
      | undefined;
    if (row === undefined) return null;
    return Buffer.from(row.chunk ?? new Uint8Array(0));
  }

  list(): ImmutableObjectMeta[] {
    const rows = this.db
      .prepare('SELECT id, size, etag, last_modified, content_type FROM objects ORDER BY id')
      .all() as Array<Omit<ObjectRow, 'data'>>;
    return rows.map((row) => rowToMeta(row as ObjectRow));
  }

  close(): void {
    this.db.close();
  }
}
