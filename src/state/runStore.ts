import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import type {
  CompositeContract,
  CompositeResponse,
  NodeExecutionRecord,
} from '../types.js';
import type { KernelEvent } from '../kernel/events.js';

/**
 * ============================================================================
 * State adapter: SQLite-backed run log.
 *
 * Persists one row per composite run plus per-node execution records, so a
 * problem can be replayed from the log: run id, request params, contract
 * shape, key intermediate node states, field-level reasons and the verdict.
 * ============================================================================
 */

export interface RunSummaryRow {
  run_id: string;
  contract_name: string;
  contract_version: number;
  outcome: string;
  snapshot_token: string | null;
  started_at: number;
  duration_ms: number;
  request_params: string;
  failures: string;
  response_json: string;
}

export interface NodeRow {
  run_id: string;
  node_id: string;
  source: string;
  status: string;
  attempts: number;
  latency_ms: number | null;
  snapshot_token_used: string | null;
  snapshot_honoured: number;
  did_not_invoke: number;
  failure_json: string | null;
}

export class RunStore {
  private readonly db: DatabaseSync;

  constructor(path = ':memory:') {
    if (path !== ':memory:') {
      mkdirSync(dirname(path), { recursive: true });
    }
    this.db = new DatabaseSync(path);
    this.db.exec('PRAGMA journal_mode = WAL;');
    this.db.exec('PRAGMA foreign_keys = ON;');
    this.initSchema();
  }

  private initSchema(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS runs (
        run_id           TEXT PRIMARY KEY,
        contract_name    TEXT NOT NULL,
        contract_version INTEGER NOT NULL,
        outcome          TEXT NOT NULL,
        snapshot_token   TEXT,
        started_at       INTEGER NOT NULL,
        duration_ms      INTEGER NOT NULL,
        request_params   TEXT NOT NULL,
        failures         TEXT NOT NULL,
        response_json    TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS run_nodes (
        run_id              TEXT NOT NULL REFERENCES runs(run_id),
        node_id             TEXT NOT NULL,
        source              TEXT NOT NULL,
        status              TEXT NOT NULL,
        attempts            INTEGER NOT NULL,
        latency_ms          INTEGER,
        snapshot_token_used TEXT,
        snapshot_honoured   INTEGER NOT NULL,
        did_not_invoke      INTEGER NOT NULL,
        failure_json        TEXT,
        PRIMARY KEY (run_id, node_id)
      );
      CREATE INDEX IF NOT EXISTS idx_run_nodes_status
        ON run_nodes(run_id, status);
      CREATE TABLE IF NOT EXISTS run_events (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        seq    INTEGER NOT NULL,
        ts     INTEGER NOT NULL,
        type   TEXT NOT NULL,
        event_json TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
      );
    `);
  }

  /** Insert the run row up front (outcome='running') so events can reference it. */
  startRun(input: {
    runId: string;
    contract: CompositeContract;
    snapshotToken: string | null;
    startedAt: number;
    deadline: number;
    requestParams: Record<string, unknown>;
  }): void {
    this.db
      .prepare(
        `INSERT OR REPLACE INTO runs
           (run_id, contract_name, contract_version, outcome, snapshot_token,
            started_at, duration_ms, request_params, failures, response_json)
         VALUES (?, ?, ?, 'running', ?, ?, 0, ?, '[]', '{}')`,
      )
      .run(
        input.runId,
        input.contract.name,
        input.contract.version,
        input.snapshotToken,
        input.startedAt,
        JSON.stringify(input.requestParams),
      );
  }

  /** EventSink implementation: persist an ordered kernel event. */
  recordEvent(event: KernelEvent): void {
    this.db
      .prepare(
        `INSERT INTO run_events (run_id, seq, ts, type, event_json)
         VALUES (?, ?, ?, ?, ?)`,
      )
      .run(event.runId, event.seq, event.ts, event.type, JSON.stringify(event));
  }

  getEvents(runId: string): KernelEvent[] {
    const rows = this.db
      .prepare('SELECT event_json FROM run_events WHERE run_id = ? ORDER BY seq')
      .all(runId) as unknown as Array<{ event_json: string }>;
    return rows.map((r) => JSON.parse(r.event_json) as KernelEvent);
  }

  persist(
    response: CompositeResponse,
    contract: CompositeContract,
    requestParams: Record<string, unknown>,
    nodes: NodeExecutionRecord[],
  ): void {
    const insertRun = this.db.prepare(`
      INSERT OR REPLACE INTO runs
        (run_id, contract_name, contract_version, outcome, snapshot_token,
         started_at, duration_ms, request_params, failures, response_json)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);
    const insertNode = this.db.prepare(`
      INSERT OR REPLACE INTO run_nodes
        (run_id, node_id, source, status, attempts, latency_ms,
         snapshot_token_used, snapshot_honoured, did_not_invoke, failure_json)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);

    const saveAll = (): void => {
      this.db.exec('BEGIN');
      try {
        insertRun.run(
          response.runId,
          contract.name,
          contract.version,
          response.outcome,
          response.snapshotToken,
          response.startedAt,
          response.durationMs,
          JSON.stringify(requestParams),
          JSON.stringify(response.failures),
          JSON.stringify(response),
        );
        for (const n of nodes) {
          insertNode.run(
            response.runId,
            n.nodeId,
            n.source,
            n.status,
            n.attempts,
            n.latencyMs ?? null,
            n.snapshotTokenUsed,
            n.snapshotHonoured ? 1 : 0,
            n.didNotInvoke ? 1 : 0,
            n.failure ? JSON.stringify(n.failure) : null,
          );
        }
        this.db.exec('COMMIT');
      } catch (error) {
        this.db.exec('ROLLBACK');
        throw error;
      }
    };
    saveAll();
  }

  getRun(runId: string): RunSummaryRow | undefined {
    return this.db
      .prepare('SELECT * FROM runs WHERE run_id = ?')
      .get(runId) as unknown as RunSummaryRow | undefined;
  }

  getRunResponse(runId: string): CompositeResponse | undefined {
    const row = this.getRun(runId);
    return row ? (JSON.parse(row.response_json) as CompositeResponse) : undefined;
  }

  getNodes(runId: string): NodeRow[] {
    return this.db
      .prepare('SELECT * FROM run_nodes WHERE run_id = ? ORDER BY node_id')
      .all(runId) as unknown as NodeRow[];
  }

  listRuns(limit = 50): RunSummaryRow[] {
    return this.db
      .prepare('SELECT * FROM runs ORDER BY started_at DESC LIMIT ?')
      .all(limit) as unknown as RunSummaryRow[];
  }

  /** Counts useful for assertions / diagnostics. */
  nodeStatusCounts(runId: string): Record<string, number> {
    const rows = this.db
      .prepare('SELECT status, COUNT(*) AS n FROM run_nodes WHERE run_id = ? GROUP BY status')
      .all(runId) as unknown as Array<{ status: string; n: number }>;
    return Object.fromEntries(rows.map((r) => [r.status, r.n]));
  }

  close(): void {
    this.db.close();
  }
}
