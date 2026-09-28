/**
 * SQLite state adapter.
 *
 * Persistence model: every accepted write appends an immutable row to
 * `resource_versions` (deletes append a tombstone). The current
 * representation is the highest-version, non-deleted row.
 *
 * Concurrency: WAL journaling plus `BEGIN IMMEDIATE` write transactions and a
 * busy timeout mean concurrent writers serialize at the database lock; the
 * loser's transaction starts after the winner commits and therefore sees the
 * new version, so its conditional check is evaluated against fresh state —
 * no lost updates.
 */
import Database from 'better-sqlite3';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { NewVersion, ResourceStore, StoredVersion, StoreTransaction } from '../core/ports.js';

interface VersionRow {
  resource_id: string;
  version: number;
  body_json: string;
  created_ms: number;
  deleted: number;
}

interface CountRow {
  count: number;
}

export interface SqliteStoreOptions {
  readonly busyTimeoutMs?: number;
}

export class SqliteResourceStore implements ResourceStore {
  private readonly db: Database.Database;

  constructor(filename: string, options: SqliteStoreOptions = {}) {
    mkdirSync(path.dirname(path.resolve(filename)), { recursive: true });
    this.db = new Database(filename);
    this.db.pragma('journal_mode = WAL');
    this.db.pragma('foreign_keys = ON');
    this.db.pragma('busy_timeout = ' + (options.busyTimeoutMs ?? 5_000));
    this.db.pragma('synchronous = NORMAL');
    this.migrate();
  }

  close(): void {
    this.db.close();
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS resource_versions (
        resource_id TEXT    NOT NULL,
        version     INTEGER NOT NULL,
        body_json   TEXT    NOT NULL,
        created_ms  INTEGER NOT NULL,
        deleted     INTEGER NOT NULL CHECK (deleted IN (0, 1)),
        PRIMARY KEY (resource_id, version)
      ) WITHOUT ROWID;
      CREATE INDEX IF NOT EXISTS idx_versions_resource
        ON resource_versions (resource_id, version DESC);
    `);
  }

  transact<T>(fn: (tx: StoreTransaction) => T): T {
    // BEGIN IMMEDIATE acquires the write lock up front: two concurrent
    // conditional writes can never both be inspecting the same old version.
    const transaction = this.db.transaction((): T => fn(this.adapter()));
    return transaction.immediate();
  }

  listVersions(resourceId: string): StoredVersion[] {
    const rows = this.db
      .prepare('SELECT * FROM resource_versions WHERE resource_id = ? ORDER BY version ASC')
      .all(resourceId) as VersionRow[];
    return rows.map(hydrate);
  }

  selectVersion(resourceId: string, version: number): StoredVersion | null {
    const row = this.db
      .prepare('SELECT * FROM resource_versions WHERE resource_id = ? AND version = ?')
      .get(resourceId, version) as VersionRow | undefined;
    return row ? hydrate(row) : null;
  }

  countResources(): number {
    const row = this.db
      .prepare('SELECT COUNT(DISTINCT resource_id) AS count FROM resource_versions')
      .get() as CountRow;
    return row.count;
  }

  listCurrent(limit: number, offset: number): StoredVersion[] {
    // Current row per resource is its highest version; keep it only when live.
    const rows = this.db
      .prepare(
        `SELECT v.*
           FROM resource_versions v
           JOIN (
             SELECT resource_id, MAX(version) AS max_version
               FROM resource_versions
              GROUP BY resource_id
           ) m ON m.resource_id = v.resource_id AND v.version = m.max_version
          WHERE v.deleted = 0
          ORDER BY v.resource_id ASC
          LIMIT ? OFFSET ?`
      )
      .all(limit, offset) as VersionRow[];
    return rows.map(hydrate);
  }

  /** Raw prepared statements behind the StoreTransaction port. */
  private adapter(): StoreTransaction {
    const selectCurrent = this.db.prepare(
      'SELECT * FROM resource_versions WHERE resource_id = ? ORDER BY version DESC LIMIT 1'
    );
    const selectMaxVersion = this.db.prepare(
      'SELECT COALESCE(MAX(version), 0) AS max_version FROM resource_versions WHERE resource_id = ?'
    );
    const insertVersion = this.db.prepare(
      `INSERT INTO resource_versions (resource_id, version, body_json, created_ms, deleted)
       VALUES (@resource_id, @version, @body_json, @created_ms, @deleted)`
    );
    return {
      selectCurrent: (resourceId: string): StoredVersion | null => {
        const row = selectCurrent.get(resourceId) as VersionRow | undefined;
        if (!row || row.deleted === 1) return null;
        return hydrate(row);
      },
      selectMaxVersion: (resourceId: string): number => {
        const row = selectMaxVersion.get(resourceId) as { max_version: number };
        return row.max_version;
      },
      insertVersion: (input: NewVersion): StoredVersion => {
        const persisted = {
          resource_id: input.resourceId,
          version: input.version,
          body_json: JSON.stringify(input.body),
          created_ms: input.createdMs,
          deleted: input.deleted ? 1 : 0
        };
        insertVersion.run(persisted);
        // Read-back from the table: the response is built from persisted bytes.
        const row = this.db
          .prepare('SELECT * FROM resource_versions WHERE resource_id = ? AND version = ?')
          .get(input.resourceId, input.version) as VersionRow;
        return hydrate(row);
      }
    };
  }
}

function hydrate(row: VersionRow): StoredVersion {
  return {
    resourceId: row.resource_id,
    version: row.version,
    body: JSON.parse(row.body_json) as unknown,
    createdMs: row.created_ms,
    deleted: row.deleted === 1
  };
}
