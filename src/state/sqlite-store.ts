import Database from 'better-sqlite3';
import type { Database as DB } from 'better-sqlite3';
import type {
  AppendEventArgs,
  DiagnosticDecision,
  DiagnosticEvent,
  EventFilter,
  FinishOutcome,
  OperationFilter,
  OperationRecord,
  StartOperationInput,
  StateStore,
} from './store.js';
import { IdempotencyInsertConflict } from './store.js';
import type { JsonValue } from '../contract/protocol.js';

/**
 * Durable SQLite adapter. All writes use WAL + synchronous transactions so a
 * notification that fails still leaves an operation row behind even when the
 * HTTP connection drops before the client can read a response.
 *
 * Idempotency is enforced per METHOD with an explicit client key — never by
 * RPC id (the same id may legitimately appear in two unrelated batches, and
 * must not collide across them).
 */
export class SqliteStateStore implements StateStore {
  private db: DB;

  constructor(path: string) {
    this.db = new Database(path);
    this.db.pragma('journal_mode = WAL');
    this.db.pragma('foreign_keys = ON');
  }

  init(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS operations (
        operation_id    TEXT PRIMARY KEY,
        batch_id        TEXT,
        connection_id   TEXT,
        method          TEXT NOT NULL,
        idempotency_key TEXT,
        fingerprint     TEXT,
        replayed_from_operation_id TEXT,
        status          TEXT NOT NULL CHECK (status IN ('pending','succeeded','failed','cancelled')),
        request         TEXT,
        result          TEXT,
        error_code      INTEGER,
        error_message   TEXT,
        started_at      INTEGER NOT NULL,
        finished_at     INTEGER,
        duration_ms     INTEGER,
        UNIQUE(method, idempotency_key)
      );
      CREATE INDEX IF NOT EXISTS idx_op_status ON operations(status);
      CREATE INDEX IF NOT EXISTS idx_op_method ON operations(method);
      CREATE INDEX IF NOT EXISTS idx_op_started ON operations(started_at);

      CREATE TABLE IF NOT EXISTS diagnostic_events (
        event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            INTEGER NOT NULL,
        connection_id TEXT,
        batch_id      TEXT,
        rpc_id        TEXT,
        operation_id  TEXT,
        kind          TEXT NOT NULL,
        decision      TEXT NOT NULL CHECK (decision IN ('accepted','rejected','indeterminate')),
        reason        TEXT NOT NULL,
        detail        TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_ev_batch ON diagnostic_events(batch_id);
      CREATE INDEX IF NOT EXISTS idx_ev_conn ON diagnostic_events(connection_id);
      CREATE INDEX IF NOT EXISTS idx_ev_op ON diagnostic_events(operation_id);
    `);
  }

  startOperation(input: StartOperationInput): void {
    try {
      this.db
        .prepare(
          `INSERT INTO operations
             (operation_id, batch_id, connection_id, method, idempotency_key, fingerprint,
              replayed_from_operation_id,
              status, request, result, error_code, error_message,
              started_at, finished_at, duration_ms)
           VALUES
             (@operationId, @batchId, @connectionId, @method, @idempotencyKey, @fingerprint,
              @replayedFromOperationId,
              'pending', @request, NULL, NULL, NULL,
              @startedAt, NULL, NULL)`,
        )
        .run({
          operationId: input.operationId,
          batchId: input.batchId,
          connectionId: input.connectionId,
          method: input.method,
          idempotencyKey: input.idempotencyKey,
          fingerprint: input.fingerprint,
          replayedFromOperationId: input.replayedFromOperationId ?? null,
          request: encode(input.redactedRequest),
          startedAt: input.startedAt,
        });
    } catch (err) {
      if (isIdempotencyConflict(err)) {
        throw new IdempotencyInsertConflict(
          input.method,
          input.idempotencyKey ?? '<null>',
        );
      }
      throw err;
    }
  }

  finishOperation(operationId: string, outcome: FinishOutcome): void {
    const duration = this.db
      .prepare('SELECT started_at FROM operations WHERE operation_id = ?')
      .get(operationId) as { started_at: number } | undefined;
    if (!duration) throw new Error(`unknown operation id ${operationId}`);

    if (outcome.status === 'succeeded') {
      this.db
        .prepare(
          `UPDATE operations
             SET status = 'succeeded', result = @result,
                 error_code = NULL, error_message = NULL,
                 finished_at = @finishedAt, duration_ms = @duration
           WHERE operation_id = @operationId`,
        )
        .run({
          operationId,
          result: encode(outcome.result),
          finishedAt: outcome.finishedAt,
          duration: outcome.finishedAt - duration.started_at,
        });
    } else {
      this.db
        .prepare(
          `UPDATE operations
             SET status = @status, result = NULL,
                 error_code = @errorCode, error_message = @errorMessage,
                 finished_at = @finishedAt, duration_ms = @duration
           WHERE operation_id = @operationId`,
        )
        .run({
          operationId,
          status: outcome.status,
          errorCode: outcome.errorCode,
          errorMessage: outcome.errorMessage,
          finishedAt: outcome.finishedAt,
          duration: outcome.finishedAt - duration.started_at,
        });
    }
  }

  getOperation(operationId: string): OperationRecord | null {
    const row = this.db
      .prepare('SELECT * FROM operations WHERE operation_id = ?')
      .get(operationId) as OperationRow | undefined;
    return row ? rowToRecord(row) : null;
  }

  findByIdempotencyKey(method: string, key: string): OperationRecord | null {
    const row = this.db
      .prepare('SELECT * FROM operations WHERE method = ? AND idempotency_key = ?')
      .get(method, key) as OperationRow | undefined;
    return row ? rowToRecord(row) : null;
  }

  listOperations(filter: OperationFilter = {}): OperationRecord[] {
    const clauses: string[] = [];
    const params: Record<string, unknown> = {};
    if (filter.status) {
      clauses.push('status = @status');
      params.status = filter.status;
    }
    if (filter.method) {
      clauses.push('method = @method');
      params.method = filter.method;
    }
    if (filter.idempotencyKey) {
      clauses.push('idempotency_key = @idempotencyKey');
      params.idempotencyKey = filter.idempotencyKey;
    }
    const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
    const limit = filter.limit && filter.limit > 0 ? `LIMIT ${Math.floor(filter.limit)}` : '';
    const rows = this.db
      .prepare(
        `SELECT * FROM operations ${where} ORDER BY started_at DESC, operation_id DESC ${limit}`,
      )
      .all(params) as OperationRow[];
    return rows.map(rowToRecord);
  }

  appendEvent(args: AppendEventArgs): DiagnosticEvent {
    // Compute ts ONCE: the returned object and the persisted row must agree
    // even across a millisecond boundary (matches MemoryStateStore).
    const ts = args.ts ?? Date.now();
    const info = this.db
      .prepare(
        `INSERT INTO diagnostic_events
           (ts, connection_id, batch_id, rpc_id, operation_id, kind, decision, reason, detail)
         VALUES
           (@ts, @connectionId, @batchId, @rpcId, @operationId, @kind, @decision, @reason, @detail)`,
      )
      .run({
        ts,
        connectionId: args.connectionId ?? null,
        batchId: args.batchId ?? null,
        rpcId: args.rpcId ?? null,
        operationId: args.operationId ?? null,
        kind: args.kind,
        decision: args.decision,
        reason: args.reason,
        detail: encode(args.detail ?? null),
      });
    return {
      eventId: Number(info.lastInsertRowid),
      ts,
      connectionId: args.connectionId ?? null,
      batchId: args.batchId ?? null,
      rpcId: args.rpcId ?? null,
      operationId: args.operationId ?? null,
      kind: args.kind,
      decision: args.decision as DiagnosticDecision,
      reason: args.reason,
      detail: args.detail ?? null,
    };
  }

  listEvents(filter: EventFilter = {}): DiagnosticEvent[] {
    const clauses: string[] = [];
    const params: Record<string, unknown> = {};
    if (filter.batchId) {
      clauses.push('batch_id = @batchId');
      params.batchId = filter.batchId;
    }
    if (filter.connectionId) {
      clauses.push('connection_id = @connectionId');
      params.connectionId = filter.connectionId;
    }
    if (filter.operationId) {
      clauses.push('operation_id = @operationId');
      params.operationId = filter.operationId;
    }
    const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
    // Match the memory adapter exactly: return the NEWEST `limit` events but
    // present them in ascending (chronological) order. A plain ASC LIMIT would
    // return the OLDEST N, diverging from MemoryStateStore.
    const limitN = filter.limit && filter.limit > 0 ? Math.floor(filter.limit) : null;
    const newestSubquery =
      limitN !== null
        ? `SELECT * FROM diagnostic_events ${where} ORDER BY event_id DESC LIMIT ${limitN}`
        : `SELECT * FROM diagnostic_events ${where}`;
    const rows = this.db
      .prepare(`SELECT * FROM (${newestSubquery}) ORDER BY event_id ASC`)
      .all(params) as EventRow[];
    return rows.map(rowToEvent);
  }

  close(): void {
    this.db.close();
  }
}

interface OperationRow {
  operation_id: string;
  batch_id: string | null;
  connection_id: string | null;
  method: string;
  idempotency_key: string | null;
  fingerprint: string | null;
  replayed_from_operation_id: string | null;
  status: OperationRecord['status'];
  request: string | null;
  result: string | null;
  error_code: number | null;
  error_message: string | null;
  started_at: number;
  finished_at: number | null;
  duration_ms: number | null;
}

interface EventRow {
  event_id: number;
  ts: number;
  connection_id: string | null;
  batch_id: string | null;
  rpc_id: string | null;
  operation_id: string | null;
  kind: string;
  decision: DiagnosticDecision;
  reason: string;
  detail: string | null;
}

function rowToRecord(row: OperationRow): OperationRecord {
  return {
    operationId: row.operation_id,
    batchId: row.batch_id,
    connectionId: row.connection_id,
    method: row.method,
    idempotencyKey: row.idempotency_key,
    fingerprint: row.fingerprint,
    replayedFromOperationId: row.replayed_from_operation_id,
    status: row.status,
    request: decode(row.request),
    result: decode(row.result),
    errorCode: row.error_code,
    errorMessage: row.error_message,
    startedAt: row.started_at,
    finishedAt: row.finished_at,
    durationMs: row.duration_ms,
  };
}

function rowToEvent(row: EventRow): DiagnosticEvent {
  return {
    eventId: row.event_id,
    ts: row.ts,
    connectionId: row.connection_id,
    batchId: row.batch_id,
    rpcId: row.rpc_id,
    operationId: row.operation_id,
    kind: row.kind,
    decision: row.decision,
    reason: row.reason,
    detail: decode(row.detail),
  };
}

function encode(value: JsonValue | null): string | null {
  if (value === null || value === undefined) return null;
  return JSON.stringify(value);
}

function decode(raw: string | null): JsonValue | null {
  if (raw === null) return null;
  return JSON.parse(raw) as JsonValue;
}

/**
 * SQLite raises SQLITE_CONSTRAINT_UNIQUE for the (method, idempotency_key)
 * unique index. NULLs are excluded by that index semantics — in SQLite each
 * NULL is distinct, which is exactly the "no key -> no dedup" behavior we want.
 */
function isIdempotencyConflict(err: unknown): boolean {
  if (typeof err !== 'object' || err === null || !('code' in err)) return false;
  const code = (err as { code?: string }).code;
  return code === 'SQLITE_CONSTRAINT_UNIQUE';
}
