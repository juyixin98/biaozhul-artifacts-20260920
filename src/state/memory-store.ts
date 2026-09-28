import type {
  AppendEventArgs,
  DiagnosticEvent,
  EventFilter,
  FinishOutcome,
  OperationFilter,
  OperationRecord,
  StartOperationInput,
  StateStore,
} from './store.js';
import { IdempotencyInsertConflict } from './store.js';

/**
 * In-memory StateStore. Mirrors SqliteStateStore semantics exactly; used by
 * kernel unit tests so those tests exercise the kernel, not SQL.
 */
export class MemoryStateStore implements StateStore {
  private operations = new Map<string, OperationRecord>();
  private events: DiagnosticEvent[] = [];
  private nextEventId = 1;
  private now: () => number;

  constructor(now: () => number = Date.now) {
    this.now = now;
  }

  init(): void {
    /* nothing to initialize */
  }

  startOperation(input: StartOperationInput): void {
    if (this.operations.has(input.operationId)) {
      throw new Error(`duplicate operation id ${input.operationId}`);
    }
    // Mirror SQLite: NULL keys never collide (each NULL is distinct).
    if (input.idempotencyKey !== null) {
      for (const rec of this.operations.values()) {
        if (rec.method === input.method && rec.idempotencyKey === input.idempotencyKey) {
          throw new IdempotencyInsertConflict(input.method, input.idempotencyKey);
        }
      }
    }
    this.operations.set(input.operationId, {
      operationId: input.operationId,
      batchId: input.batchId,
      connectionId: input.connectionId,
      method: input.method,
      idempotencyKey: input.idempotencyKey,
      fingerprint: input.fingerprint,
      replayedFromOperationId: input.replayedFromOperationId ?? null,
      status: 'pending',
      request: input.redactedRequest,
      result: null,
      errorCode: null,
      errorMessage: null,
      startedAt: input.startedAt,
      finishedAt: null,
      durationMs: null,
    });
  }

  finishOperation(operationId: string, outcome: FinishOutcome): void {
    const rec = this.operations.get(operationId);
    if (!rec) throw new Error(`unknown operation id ${operationId}`);
    const next: OperationRecord = {
      ...rec,
      status: outcome.status,
      finishedAt: outcome.finishedAt,
      durationMs: outcome.finishedAt - rec.startedAt,
      ...(outcome.status === 'succeeded'
        ? { result: outcome.result, errorCode: null, errorMessage: null }
        : {
            result: null,
            errorCode: outcome.errorCode,
            errorMessage: outcome.errorMessage,
          }),
    };
    this.operations.set(operationId, next);
  }

  getOperation(operationId: string): OperationRecord | null {
    const rec = this.operations.get(operationId);
    return rec ? { ...rec } : null;
  }

  findByIdempotencyKey(method: string, key: string): OperationRecord | null {
    for (const rec of this.operations.values()) {
      if (rec.method === method && rec.idempotencyKey === key) return { ...rec };
    }
    return null;
  }

  listOperations(filter: OperationFilter = {}): OperationRecord[] {
    const rows = [...this.operations.values()]
      .filter((r) => (filter.status ? r.status === filter.status : true))
      .filter((r) => (filter.method ? r.method === filter.method : true))
      .filter((r) => (filter.idempotencyKey ? r.idempotencyKey === filter.idempotencyKey : true))
      // Tie-break must match SqliteStateStore (started_at DESC, operation_id DESC).
      .sort((a, b) => b.startedAt - a.startedAt || (a.operationId < b.operationId ? 1 : -1));
    return (filter.limit ? rows.slice(0, filter.limit) : rows).map((r) => ({ ...r }));
  }

  appendEvent(args: AppendEventArgs): DiagnosticEvent {
    const event: DiagnosticEvent = {
      eventId: this.nextEventId++,
      ts: args.ts ?? this.now(),
      connectionId: args.connectionId ?? null,
      batchId: args.batchId ?? null,
      rpcId: args.rpcId ?? null,
      operationId: args.operationId ?? null,
      kind: args.kind,
      decision: args.decision,
      reason: args.reason,
      detail: args.detail ?? null,
    };
    this.events.push(event);
    return { ...event };
  }

  listEvents(filter: EventFilter = {}): DiagnosticEvent[] {
    const rows = this.events
      .filter((e) => (filter.batchId ? e.batchId === filter.batchId : true))
      .filter((e) => (filter.connectionId ? e.connectionId === filter.connectionId : true))
      .filter((e) => (filter.operationId ? e.operationId === filter.operationId : true))
      .sort((a, b) => a.eventId - b.eventId);
    return filter.limit ? rows.slice(-filter.limit) : rows;
  }

  close(): void {
    this.operations.clear();
    this.events = [];
  }
}
