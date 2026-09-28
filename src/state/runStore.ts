/**
 * State adapter (状态适配) — SQLite persistence.
 *
 * Three concerns:
 *  1. `runs`       – one row per composite execution (status + full envelope);
 *  2. `run_events` – structured, replayable event stream (powered by RunLog);
 *  3. `idempotency`– Idempotency-Key -> runId binding with request fingerprint.
 *
 * A reused key with a DIFFERENT fingerprint is a STATE_CONFLICT (409); a reused
 * key with the SAME fingerprint replays the original run instead of re-executing.
 */
import Database from 'better-sqlite3';
import { createHash } from 'node:crypto';
import type { Database as DatabaseType } from 'better-sqlite3';
import type { LogEvent, EventSink } from '../observability/runLog.js';
import type { CompositeResult } from '../kernel/types.js';
import { stateConflict } from '../kernel/errors.js';

interface RunRow {
  run_id: string;
  contract: string;
  status: string;
  snapshot_token: string;
  started_at: number;
  ended_at: number;
  deadline_ms: number;
  result_json: string;
}

interface IdempotencyRow {
  idem_key: string;
  fingerprint: string;
  run_id: string;
  created_at: number;
}

export class RunStore {
  private readonly db: DatabaseType;

  constructor(path: string) {
    this.db = new Database(path);
    this.db.pragma('journal_mode = WAL');
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS runs (
        run_id         TEXT PRIMARY KEY,
        contract       TEXT NOT NULL,
        status         TEXT NOT NULL,
        snapshot_token TEXT NOT NULL,
        started_at     INTEGER NOT NULL,
        ended_at       INTEGER NOT NULL,
        deadline_ms    INTEGER NOT NULL,
        result_json    TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS run_events (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id  TEXT NOT NULL,
        seq     INTEGER NOT NULL,
        ts      INTEGER NOT NULL,
        type    TEXT NOT NULL,
        node    TEXT,
        message TEXT NOT NULL,
        data    TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, seq);
      CREATE TABLE IF NOT EXISTS idempotency (
        idem_key    TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        run_id      TEXT NOT NULL,
        created_at  INTEGER NOT NULL
      );
    `);
  }

  close(): void {
    this.db.close();
  }

  /** Event sink for RunLog: durable append of every intermediate state. */
  eventSink(): EventSink {
    const insert = this.db.prepare(
      `INSERT INTO run_events (run_id, seq, ts, type, node, message, data)
       VALUES (@runId, @seq, @ts, @type, @node, @message, @data)`,
    );
    return (event: LogEvent) => {
      insert.run({
        runId: event.runId,
        seq: event.seq,
        ts: event.ts,
        type: event.type,
        node: event.node ?? null,
        message: event.message,
        data: event.data !== undefined ? JSON.stringify(event.data) : null,
      });
    };
  }

  saveRun(result: CompositeResult): void {
    const row: RunRow = {
      run_id: result.runId,
      contract: result.contract,
      status: result.status,
      snapshot_token: result.snapshotToken,
      started_at: result.startedAt,
      ended_at: result.endedAt,
      deadline_ms: result.deadlineMs,
      result_json: JSON.stringify(result),
    };
    this.db
      .prepare(
        `INSERT OR REPLACE INTO runs
           (run_id, contract, status, snapshot_token, started_at, ended_at, deadline_ms, result_json)
         VALUES
           (@run_id, @contract, @status, @snapshot_token, @started_at, @ended_at, @deadline_ms, @result_json)`,
      )
      .run(row);
  }

  getRun(runId: string): CompositeResult | null {
    const row = this.db.prepare(`SELECT * FROM runs WHERE run_id = ?`).get(runId) as
      | RunRow
      | undefined;
    return row ? (JSON.parse(row.result_json) as CompositeResult) : null;
  }

  listRuns(limit = 50): Array<Pick<CompositeResult, 'runId' | 'contract' | 'status' | 'snapshotToken' | 'startedAt' | 'endedAt'>> {
    const rows = this.db
      .prepare(
        `SELECT run_id, contract, status, snapshot_token, started_at, ended_at
           FROM runs ORDER BY started_at DESC LIMIT ?`,
      )
      .all(limit) as Array<{
      run_id: string;
      contract: string;
      status: string;
      snapshot_token: string;
      started_at: number;
      ended_at: number;
    }>;
    return rows.map((r) => ({
      runId: r.run_id,
      contract: r.contract,
      status: r.status as CompositeResult['status'],
      snapshotToken: r.snapshot_token,
      startedAt: r.started_at,
      endedAt: r.ended_at,
    }));
  }

  getEvents(runId: string): LogEvent[] {
    const rows = this.db
      .prepare(
        `SELECT seq, ts, type, node, message, data FROM run_events WHERE run_id = ? ORDER BY seq`,
      )
      .all(runId) as Array<{
      seq: number;
      ts: number;
      type: string;
      node: string | null;
      message: string;
      data: string | null;
    }>;
    return rows.map((r) => ({
      seq: r.seq,
      ts: r.ts,
      runId,
      type: r.type,
      ...(r.node !== null ? { node: r.node } : {}),
      message: r.message,
      ...(r.data !== null ? { data: JSON.parse(r.data) as Record<string, unknown> } : {}),
    }));
  }

  static fingerprint(payload: string): string {
    return createHash('sha256').update(payload).digest('hex');
  }

  /**
   * Reserve an idempotency key.
   * Returns the existing runId when the fingerprint matches; throws
   * STATE_CONFLICT when the same key arrives with a different payload.
   */
  reserveIdempotency(key: string, fingerprint: string, runId: string): { runId: string; replayed: boolean } {
    const existing = this.db
      .prepare(`SELECT * FROM idempotency WHERE idem_key = ?`)
      .get(key) as IdempotencyRow | undefined;
    if (existing) {
      if (existing.fingerprint !== fingerprint) {
        throw stateConflict(
          'IDEMPOTENCY_KEY_MISMATCH',
          `idempotency key ${key} was already used with a different request payload`,
          { key, existingRunId: existing.run_id },
        );
      }
      return { runId: existing.run_id, replayed: true };
    }
    this.db
      .prepare(
        `INSERT INTO idempotency (idem_key, fingerprint, run_id, created_at)
         VALUES (?, ?, ?, ?)`,
      )
      .run(key, fingerprint, runId, Date.now());
    return { runId, replayed: false };
  }
}
