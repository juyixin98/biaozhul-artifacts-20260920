import Database from 'better-sqlite3';
import type { Database as DB } from 'better-sqlite3';
import type { DomainStore, KvEntry } from './domain-store.js';
import { DuplicateKeyError } from './domain-store.js';
import type { JsonValue } from '../contract/protocol.js';

/**
 * Durable domain fixture. The unique constraint on key is the real guard
 * against duplicate side effects; the operation idempotency layer sits above
 * it and gives better errors before hitting this constraint.
 */
export class SqliteDomainStore implements DomainStore {
  private db: DB;

  constructor(path: string, db?: DB) {
    // Tests can share a connection to an in-memory database.
    this.db = db ?? new Database(path);
  }

  init(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS kv_entries (
        key          TEXT PRIMARY KEY,
        value        TEXT NOT NULL,
        revision     INTEGER NOT NULL,
        operation_id TEXT NOT NULL,
        created_at   INTEGER NOT NULL
      );
    `);
  }

  put(entry: KvEntry): void {
    try {
      this.db
        .prepare(
          `INSERT INTO kv_entries (key, value, revision, operation_id, created_at)
           VALUES (@key, @value, @revision, @operationId, @createdAt)`,
        )
        .run({
          key: entry.key,
          value: JSON.stringify(entry.value),
          revision: entry.revision,
          operationId: entry.operationId,
          createdAt: entry.createdAt,
        });
    } catch (err) {
      if (isUniqueViolation(err)) throw new DuplicateKeyError(entry.key);
      throw err;
    }
  }

  get(key: string): KvEntry | null {
    const row = this.db.prepare('SELECT * FROM kv_entries WHERE key = ?').get(key) as
      | KvRow
      | undefined;
    return row ? toEntry(row) : null;
  }

  list(): KvEntry[] {
    const rows = this.db
      .prepare('SELECT * FROM kv_entries ORDER BY revision ASC')
      .all() as KvRow[];
    return rows.map(toEntry);
  }

  close(): void {
    this.db.close();
  }
}

interface KvRow {
  key: string;
  value: string;
  revision: number;
  operation_id: string;
  created_at: number;
}

function toEntry(row: KvRow): KvEntry {
  return {
    key: row.key,
    value: JSON.parse(row.value) as JsonValue,
    revision: row.revision,
    operationId: row.operation_id,
    createdAt: row.created_at,
  };
}

function isUniqueViolation(err: unknown): boolean {
  return (
    typeof err === 'object' &&
    err !== null &&
    'code' in err &&
    (err as { code?: string }).code === 'SQLITE_CONSTRAINT_PRIMARYKEY'
  );
}
