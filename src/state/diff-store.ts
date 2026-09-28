/**
 * State adapter: persists diff runs, findings and uncertainties in SQLite.
 *
 * The store is deliberately dumb — the kernel never imports it. It exists so
 * a diagnostic request can be correlated and re-fetched by request id, and so
 * logs carry stable run identifiers.
 */
import Database from 'better-sqlite3';
import type { DiffResult, Finding, Uncertainty } from '../core/types.js';

export interface RunRecord {
  request_id: string;
  old_title: string;
  new_title: string;
  old_version: string;
  new_version: string;
  compatible: number;
  created_at: string;
  breaking: number;
  non_breaking: number;
  uncertain: number;
}

export interface StoredRun extends RunRecord {
  findings: Finding[];
  uncertainties: Uncertainty[];
}

export class DiffStore {
  private db: Database.Database;

    constructor(dbPath: string) {
    this.db = new Database(dbPath);
    this.db.pragma('journal_mode = WAL');
    this.db.exec(SCHEMA);
  }

  /** Test/diagnostic helper: in-memory database. */
  static memory(): DiffStore {
    return new DiffStore(':memory:');
  }

  close(): void {
    this.db.close();
  }

  saveRun(result: DiffResult): void {
    const insertRun = this.db.prepare(`
      INSERT OR REPLACE INTO runs
        (request_id, old_title, new_title, old_version, new_version,
         compatible, created_at, breaking, non_breaking, uncertain)
      VALUES
        (@request_id, @old_title, @new_title, @old_version, @new_version,
         @compatible, @created_at, @breaking, @non_breaking, @uncertain)
    `);
    const insertFinding = this.db.prepare(`
      INSERT INTO findings
        (request_id, ord, direction, code, severity, operation, path,
         message, old_value, new_value, witness)
      VALUES
        (@request_id, @ord, @direction, @code, @severity, @operation, @path,
         @message, @old_value, @new_value, @witness)
    `);
    const insertUncertainty = this.db.prepare(`
      INSERT INTO uncertainties (request_id, ord, code, location, detail)
      VALUES (@request_id, @ord, @code, @location, @detail)
    `);

    const tx = this.db.transaction((res: DiffResult): void => {
      insertRun.run({
        request_id: res.requestId,
        old_title: res.oldTitle,
        new_title: res.newTitle,
        old_version: res.oldVersion,
        new_version: res.newVersion,
        compatible: res.compatible ? 1 : 0,
        created_at: new Date().toISOString(),
        breaking: res.stats.breaking,
        non_breaking: res.stats.nonBreaking,
        uncertain: res.stats.uncertain,
      });
      this.db.prepare('DELETE FROM findings WHERE request_id = ?').run(res.requestId);
      this.db.prepare('DELETE FROM uncertainties WHERE request_id = ?').run(res.requestId);
      res.findings.forEach((f, i) => {
        insertFinding.run({
          request_id: res.requestId,
          ord: i + 1,
          direction: f.direction,
          code: f.code,
          severity: f.severity,
          operation: f.operation,
          path: f.path,
          message: f.message,
          old_value: f.oldValue === undefined ? null : JSON.stringify(f.oldValue),
          new_value: f.newValue === undefined ? null : JSON.stringify(f.newValue),
          witness: f.witness ? JSON.stringify(f.witness) : null,
        });
      });
      res.uncertainties.forEach((u, i) => {
        insertUncertainty.run({
          request_id: res.requestId,
          ord: i + 1,
          code: u.code,
          location: u.location,
          detail: u.detail,
        });
      });
    });
    tx(result);
  }

  getRun(requestId: string): StoredRun | null {
    const run = this.db.prepare('SELECT * FROM runs WHERE request_id = ?').get(requestId) as
      | RunRecord
      | undefined;
    if (!run) return null;
    const findingRows = this.db
      .prepare('SELECT * FROM findings WHERE request_id = ? ORDER BY ord')
      .all(requestId) as Array<{
      direction: Finding['direction'];
      code: Finding['code'];
      severity: Finding['severity'];
      operation: string | null;
      path: string;
      message: string;
      old_value: string | null;
      new_value: string | null;
      witness: string | null;
      ord: number;
    }>;
    const uncertaintyRows = this.db
      .prepare('SELECT * FROM uncertainties WHERE request_id = ? ORDER BY ord')
      .all(requestId) as Array<Pick<Uncertainty, 'code' | 'location' | 'detail'>>;

    return {
      ...run,
      findings: findingRows.map((r) => ({
        id: `${requestId}:${r.ord}`,
        direction: r.direction,
        code: r.code,
        severity: r.severity,
        operation: r.operation,
        path: r.path,
        message: r.message,
        oldValue: r.old_value ? JSON.parse(r.old_value) : undefined,
        newValue: r.new_value ? JSON.parse(r.new_value) : undefined,
        witness: r.witness ? JSON.parse(r.witness) : null,
      })),
      uncertainties: uncertaintyRows.map((r) => ({
        code: r.code,
        location: r.location,
        detail: r.detail,
      })),
    };
  }

  listRuns(limit = 50): RunRecord[] {
    return this.db
      .prepare('SELECT * FROM runs ORDER BY rowid DESC LIMIT ?')
      .all(limit) as RunRecord[];
  }
}

const SCHEMA = `
CREATE TABLE IF NOT EXISTS runs (
  request_id     TEXT PRIMARY KEY,
  old_title      TEXT NOT NULL,
  new_title      TEXT NOT NULL,
  old_version    TEXT NOT NULL,
  new_version    TEXT NOT NULL,
  compatible     INTEGER NOT NULL,
  created_at     TEXT NOT NULL,
  breaking       INTEGER NOT NULL,
  non_breaking   INTEGER NOT NULL,
  uncertain      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
  request_id  TEXT NOT NULL,
  ord         INTEGER NOT NULL,
  direction   TEXT NOT NULL,
  code        TEXT NOT NULL,
  severity    TEXT NOT NULL,
  operation   TEXT,
  path        TEXT NOT NULL,
  message     TEXT NOT NULL,
  old_value   TEXT,
  new_value   TEXT,
  witness     TEXT,
  PRIMARY KEY (request_id, ord),
  FOREIGN KEY (request_id) REFERENCES runs(request_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS uncertainties (
  request_id  TEXT NOT NULL,
  ord         INTEGER NOT NULL,
  code        TEXT NOT NULL,
  location    TEXT NOT NULL,
  detail      TEXT NOT NULL,
  PRIMARY KEY (request_id, ord),
  FOREIGN KEY (request_id) REFERENCES runs(request_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
`;
