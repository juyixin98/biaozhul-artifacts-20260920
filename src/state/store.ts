import type { JsonValue } from '../contract/protocol.js';

/**
 * State adapter boundary. The kernel depends on this interface, never on
 * SQLite directly. Two real implementations live behind it:
 *  - SqliteStateStore: durable, used by the server.
 *  - MemoryStateStore: used by unit tests; same semantics, different adapter.
 */

export type OperationStatus = 'pending' | 'succeeded' | 'failed' | 'cancelled';

export type DiagnosticDecision = 'accepted' | 'rejected' | 'indeterminate';

export interface OperationRecord {
  operationId: string;
  batchId: string | null;
  connectionId: string | null;
  method: string;
  idempotencyKey: string | null;
  /** Canonical payload fingerprint; guards same key + different params. */
  fingerprint: string | null;
  /**
   * Set on REJECTED attempts (fingerprint mismatch / in-flight conflict):
   * the audit record's own operationId is new, but it points at the original
   * in-flight/completed operation. Such rows carry idempotencyKey = NULL so
   * they never disturb the (method, key) uniqueness that guards first runs.
   */
  replayedFromOperationId: string | null;
  status: OperationStatus;
  /** Params as stored: already redacted (secrets never land in the record). */
  request: JsonValue | null;
  result: JsonValue | null;
  errorCode: number | null;
  errorMessage: string | null;
  startedAt: number;
  finishedAt: number | null;
  durationMs: number | null;
}

export interface StartOperationInput {
  operationId: string;
  batchId: string | null;
  connectionId: string | null;
  method: string;
  idempotencyKey: string | null;
  fingerprint: string | null;
  replayedFromOperationId?: string | null;
  /** Must already be redacted. */
  redactedRequest: JsonValue | null;
  startedAt: number;
}

export type FinishOutcome =
  | { status: 'succeeded'; result: JsonValue; finishedAt: number }
  | { status: 'failed'; errorCode: number; errorMessage: string; finishedAt: number }
  | { status: 'cancelled'; errorCode: number; errorMessage: string; finishedAt: number };

export interface DiagnosticEvent {
  eventId: number;
  ts: number;
  connectionId: string | null;
  batchId: string | null;
  /** Rendered RPC id, or null when no trustworthy id exists. */
  rpcId: string | null;
  operationId: string | null;
  kind: string;
  decision: DiagnosticDecision;
  reason: string;
  /** Must already be redacted. */
  detail: JsonValue | null;
}

export interface AppendEventArgs {
  ts?: number;
  connectionId?: string | null;
  batchId?: string | null;
  rpcId?: string | null;
  operationId?: string | null;
  kind: string;
  decision: DiagnosticDecision;
  reason: string;
  detail?: JsonValue | null;
}

export interface OperationFilter {
  status?: OperationStatus;
  method?: string;
  idempotencyKey?: string;
  limit?: number;
}

export interface EventFilter {
  batchId?: string;
  connectionId?: string;
  operationId?: string;
  limit?: number;
}

export interface StateStore {
  init(): void;

  startOperation(input: StartOperationInput): void;
  finishOperation(operationId: string, outcome: FinishOutcome): void;
  getOperation(operationId: string): OperationRecord | null;
  findByIdempotencyKey(method: string, key: string): OperationRecord | null;
  listOperations(filter?: OperationFilter): OperationRecord[];

  appendEvent(args: AppendEventArgs): DiagnosticEvent;
  listEvents(filter?: EventFilter): DiagnosticEvent[];

  close(): void;
}

/**
 * Thrown by startOperation when (method, idempotencyKey) already exists.
 * Both adapters enforce it so concurrent first-execution races are caught
 * without depending on SQL-specific error codes.
 */
export class IdempotencyInsertConflict extends Error {
  constructor(
    readonly method: string,
    readonly idempotencyKey: string,
  ) {
    super(`idempotency key already recorded for ${method}: ${idempotencyKey}`);
    this.name = 'IdempotencyInsertConflict';
  }
}
