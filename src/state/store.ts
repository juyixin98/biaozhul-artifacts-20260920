/**
 * State adapter: SQLite-backed implementation of the LedgerStore port.
 *
 * Nothing in this module knows JSON-RPC method semantics. It persists
 * requests/calls/operations and serves diagnostic queries. Raw method
 * parameters are intentionally NEVER stored: callers hand us already-redacted
 * input summaries (redactedInput), so a secret in params cannot land in the
 * ledger or in logs via this layer.
 */

import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";

import { SCHEMA_SQL } from "./schema.js";

export type PayloadKind = "single" | "batch" | "parse_error" | "empty_batch";
export type RequestVerdict = "accepted" | "rejected" | "undecidable";
export type FailureLayer = "parse" | "contract" | "execution";
export type CallStatus =
  | "pending"
  | "success"
  | "error"
  | "invalid"
  | "interrupted";
export type OperationStatus =
  | "pending"
  | "succeeded"
  | "failed"
  | "skipped"
  | "interrupted";

export interface RequestRecord {
  readonly requestCorr: string;
  readonly receivedAt: string;
  readonly finishedAt: string | null;
  readonly payloadKind: PayloadKind;
  readonly sizeBytes: number;
  readonly callCount: number;
  readonly notificationCount: number;
  readonly invalidCount: number;
  readonly verdict: RequestVerdict;
  readonly layer: FailureLayer;
  readonly failureCategory: string | null;
  readonly reason: string | null;
}

export interface CallRecord {
  readonly callCorr: string;
  readonly requestCorr: string;
  readonly position: number;
  readonly rpcIdJson: string | null;
  readonly isNotification: boolean;
  readonly method: string | null;
  readonly startedAt: string;
  readonly finishedAt: string | null;
  readonly status: CallStatus;
  readonly errorCode: number | null;
  readonly errorCategory: string | null;
  readonly errorMessage: string | null;
}

export interface OperationRecord {
  readonly opSeq: number;
  readonly callCorr: string;
  readonly requestCorr: string;
  readonly rpcIdJson: string | null;
  readonly idempotencyKey: string | null;
  readonly kind: string;
  readonly startedAt: string;
  readonly finishedAt: string | null;
  readonly status: OperationStatus;
  readonly redactedInput: string | null;
  readonly resultJson: string | null;
  readonly errorCode: number | null;
  readonly errorCategory: string | null;
  readonly errorMessage: string | null;
}

export interface NewCall {
  readonly callCorr: string;
  readonly requestCorr: string;
  readonly position: number;
  readonly rpcIdJson: string | null;
  readonly isNotification: boolean;
  readonly method: string | null;
  readonly startedAt: string;
}

export interface NewOperation {
  readonly callCorr: string;
  readonly requestCorr: string;
  readonly rpcIdJson: string | null;
  readonly idempotencyKey: string | null;
  readonly kind: string;
  readonly startedAt: string;
  readonly redactedInput: string | null;
}

export interface OperationFilter {
  readonly status?: OperationStatus;
  readonly kind?: string;
  readonly idempotencyKey?: string;
  readonly limit?: number;
  readonly offset?: number;
}

function mapRequest(row: Record<string, unknown>): RequestRecord {
  return {
    requestCorr: row.request_corr as string,
    receivedAt: row.received_at as string,
    finishedAt: (row.finished_at as string | null) ?? null,
    payloadKind: row.payload_kind as PayloadKind,
    sizeBytes: row.size_bytes as number,
    callCount: row.call_count as number,
    notificationCount: row.notification_count as number,
    invalidCount: row.invalid_count as number,
    verdict: row.verdict as RequestVerdict,
    layer: row.layer as FailureLayer,
    failureCategory: (row.failure_category as string | null) ?? null,
    reason: (row.reason as string | null) ?? null,
  };
}

function mapCall(row: Record<string, unknown>): CallRecord {
  return {
    callCorr: row.call_corr as string,
    requestCorr: row.request_corr as string,
    position: row.position as number,
    rpcIdJson: (row.rpc_id_json as string | null) ?? null,
    isNotification: row.is_notification === 1,
    method: (row.method as string | null) ?? null,
    startedAt: row.started_at as string,
    finishedAt: (row.finished_at as string | null) ?? null,
    status: row.status as CallStatus,
    errorCode: (row.error_code as number | null) ?? null,
    errorCategory: (row.error_category as string | null) ?? null,
    errorMessage: (row.error_message as string | null) ?? null,
  };
}

function mapOperation(row: Record<string, unknown>): OperationRecord {
  return {
    opSeq: row.op_seq as number,
    callCorr: row.call_corr as string,
    requestCorr: row.request_corr as string,
    rpcIdJson: (row.rpc_id_json as string | null) ?? null,
    idempotencyKey: (row.idempotency_key as string | null) ?? null,
    kind: row.kind as string,
    startedAt: row.started_at as string,
    finishedAt: (row.finished_at as string | null) ?? null,
    status: row.status as OperationStatus,
    redactedInput: (row.redacted_input as string | null) ?? null,
    resultJson: (row.result_json as string | null) ?? null,
    errorCode: (row.error_code as number | null) ?? null,
    errorCategory: (row.error_category as string | null) ?? null,
    errorMessage: (row.error_message as string | null) ?? null,
  };
}

export interface LedgerStore {
  beginRequest(input: {
    requestCorr: string;
    receivedAt: string;
    payloadKind: PayloadKind;
    sizeBytes: number;
  }): void;
  finishRequest(input: {
    requestCorr: string;
    finishedAt: string;
    callCount: number;
    notificationCount: number;
    invalidCount: number;
    verdict: RequestVerdict;
    layer: FailureLayer;
    failureCategory?: string | null;
    reason?: string | null;
  }): void;
  beginCall(call: NewCall): void;
  finishCall(input: {
    callCorr: string;
    finishedAt: string;
    status: CallStatus;
    errorCode?: number | null;
    errorCategory?: string | null;
    errorMessage?: string | null;
  }): boolean;
  beginOperation(op: NewOperation): number;
  finishOperation(input: {
    opSeq: number;
    finishedAt: string;
    status: OperationStatus;
    resultJson?: string | null;
    errorCode?: number | null;
    errorCategory?: string | null;
    errorMessage?: string | null;
  }): boolean;
  /** Terminal-marks every still-pending call/op of a request as interrupted. */
  interruptPending(requestCorr: string, finishedAt: string): {
    calls: number;
    operations: number;
  };
  getOperation(opSeq: number): OperationRecord | null;
  findOperationByIdempotencyKey(
    idempotencyKey: string,
    kind: string,
  ): OperationRecord | null;
  findLatestOperationByIdempotencyKey(
    idempotencyKey: string,
    kind: string,
  ): OperationRecord | null;
  listOperations(filter?: OperationFilter): OperationRecord[];
  getCall(callCorr: string): CallRecord | null;
  listCallsByRequest(requestCorr: string): CallRecord[];
  getRequest(requestCorr: string): RequestRecord | null;
  listRecentRequests(limit?: number): RequestRecord[];
  close(): void;
}

export class SqliteLedgerStore implements LedgerStore {
  private readonly db: DatabaseSync;

  constructor(dbPath: string) {
    if (dbPath !== ":memory:") {
      const dir = dirname(dbPath);
      if (dir !== ".") mkdirSync(dir, { recursive: true });
    }
    this.db = new DatabaseSync(dbPath);
    this.db.exec(SCHEMA_SQL);
  }

  beginRequest(input: {
    requestCorr: string;
    receivedAt: string;
    payloadKind: PayloadKind;
    sizeBytes: number;
  }): void {
    this.db
      .prepare(
        `INSERT INTO rpc_requests
           (request_corr, received_at, payload_kind, size_bytes, verdict, layer)
         VALUES (?, ?, ?, ?, 'accepted', 'execution')`,
      )
      .run(
        input.requestCorr,
        input.receivedAt,
        input.payloadKind,
        input.sizeBytes,
      );
  }

  finishRequest(input: {
    requestCorr: string;
    finishedAt: string;
    callCount: number;
    notificationCount: number;
    invalidCount: number;
    verdict: RequestVerdict;
    layer: FailureLayer;
    failureCategory?: string | null;
    reason?: string | null;
  }): void {
    this.db
      .prepare(
        `UPDATE rpc_requests SET
           finished_at = ?, call_count = ?, notification_count = ?,
           invalid_count = ?, verdict = ?, layer = ?,
           failure_category = ?, reason = ?
         WHERE request_corr = ?`,
      )
      .run(
        input.finishedAt,
        input.callCount,
        input.notificationCount,
        input.invalidCount,
        input.verdict,
        input.layer,
        input.failureCategory ?? null,
        input.reason ?? null,
        input.requestCorr,
      );
  }

  beginCall(call: NewCall): void {
    this.db
      .prepare(
        `INSERT INTO rpc_calls
           (call_corr, request_corr, position, rpc_id_json, is_notification,
            method, started_at, status)
         VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')`,
      )
      .run(
        call.callCorr,
        call.requestCorr,
        call.position,
        call.rpcIdJson,
        call.isNotification ? 1 : 0,
        call.method,
        call.startedAt,
      );
  }

  finishCall(input: {
    callCorr: string;
    finishedAt: string;
    status: CallStatus;
    errorCode?: number | null;
    errorCategory?: string | null;
    errorMessage?: string | null;
  }): boolean {
    const result = this.db
      .prepare(
        `UPDATE rpc_calls SET
           finished_at = ?, status = ?, error_code = ?,
           error_category = ?, error_message = ?
         WHERE call_corr = ? AND status = 'pending'`,
      )
      .run(
        input.finishedAt,
        input.status,
        input.errorCode ?? null,
        input.errorCategory ?? null,
        input.errorMessage ?? null,
        input.callCorr,
      );
    return result.changes > 0;
  }

  beginOperation(op: NewOperation): number {
    const result = this.db
      .prepare(
        `INSERT INTO operations
           (call_corr, request_corr, rpc_id_json, idempotency_key, kind,
            started_at, status, redacted_input)
         VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)`,
      )
      .run(
        op.callCorr,
        op.requestCorr,
        op.rpcIdJson,
        op.idempotencyKey,
        op.kind,
        op.startedAt,
        op.redactedInput,
      );
    return Number(result.lastInsertRowid);
  }

  finishOperation(input: {
    opSeq: number;
    finishedAt: string;
    status: OperationStatus;
    resultJson?: string | null;
    errorCode?: number | null;
    errorCategory?: string | null;
    errorMessage?: string | null;
  }): boolean {
    const result = this.db
      .prepare(
        `UPDATE operations SET
           finished_at = ?, status = ?, result_json = ?,
           error_code = ?, error_category = ?, error_message = ?
         WHERE op_seq = ? AND status = 'pending'`,
      )
      .run(
        input.finishedAt,
        input.status,
        input.resultJson ?? null,
        input.errorCode ?? null,
        input.errorCategory ?? null,
        input.errorMessage ?? null,
        input.opSeq,
      );
    return result.changes > 0;
  }

  interruptPending(requestCorr: string, finishedAt: string): {
    calls: number;
    operations: number;
  } {
    // Terminal-mark only rows still pending; completed work is never
    // rewritten as interrupted.
    const calls = this.db
      .prepare(
        `UPDATE rpc_calls SET finished_at = ?, status = 'interrupted',
           error_category = 'interrupted',
           error_message = 'connection lost before response delivery'
         WHERE request_corr = ? AND status = 'pending'`,
      )
      .run(finishedAt, requestCorr);
    const operations = this.db
      .prepare(
        `UPDATE operations SET finished_at = ?, status = 'interrupted',
           error_category = 'interrupted',
           error_message = 'connection lost before operation settled'
         WHERE request_corr = ? AND status = 'pending'`,
      )
      .run(finishedAt, requestCorr);
    return { calls: Number(calls.changes), operations: Number(operations.changes) };
  }

  getOperation(opSeq: number): OperationRecord | null {
    const row = this.db
      .prepare(`SELECT * FROM operations WHERE op_seq = ?`)
      .get(opSeq) as Record<string, unknown> | undefined;
    return row ? mapOperation(row) : null;
  }

  findOperationByIdempotencyKey(
    idempotencyKey: string,
    kind: string,
  ): OperationRecord | null {
    // Only a *succeeded* original can serve a replay; a failed attempt must
    // not poison retries.
    const row = this.db
      .prepare(
        `SELECT * FROM operations
         WHERE idempotency_key = ? AND kind = ? AND status = 'succeeded'
         ORDER BY op_seq ASC LIMIT 1`,
      )
      .get(idempotencyKey, kind) as Record<string, unknown> | undefined;
    return row ? mapOperation(row) : null;
  }

  findLatestOperationByIdempotencyKey(
    idempotencyKey: string,
    kind: string,
  ): OperationRecord | null {
    // Diagnostic view: most recent attempt regardless of status, so an
    // operator can see failed replays instead of silently seeing nothing.
    const row = this.db
      .prepare(
        `SELECT * FROM operations
         WHERE idempotency_key = ? AND kind = ?
         ORDER BY op_seq DESC LIMIT 1`,
      )
      .get(idempotencyKey, kind) as Record<string, unknown> | undefined;
    return row ? mapOperation(row) : null;
  }

  listOperations(filter: OperationFilter = {}): OperationRecord[] {
    const clauses: string[] = [];
    const params: Array<string | number> = [];
    if (filter.status) {
      clauses.push("status = ?");
      params.push(filter.status);
    }
    if (filter.kind) {
      clauses.push("kind = ?");
      params.push(filter.kind);
    }
    if (filter.idempotencyKey) {
      clauses.push("idempotency_key = ?");
      params.push(filter.idempotencyKey);
    }
    const where =
      clauses.length > 0 ? `WHERE ${clauses.join(" AND ")}` : "";
    const limit = Math.min(Math.max(filter.limit ?? 100, 1), 500);
    const offset = Math.max(filter.offset ?? 0, 0);
    const rows = this.db
      .prepare(
        `SELECT * FROM operations ${where}
         ORDER BY op_seq DESC LIMIT ? OFFSET ?`,
      )
      .all(...params, limit, offset) as Record<string, unknown>[];
    return rows.map(mapOperation);
  }

  getCall(callCorr: string): CallRecord | null {
    const row = this.db
      .prepare(`SELECT * FROM rpc_calls WHERE call_corr = ?`)
      .get(callCorr) as Record<string, unknown> | undefined;
    return row ? mapCall(row) : null;
  }

  listCallsByRequest(requestCorr: string): CallRecord[] {
    const rows = this.db
      .prepare(
        `SELECT * FROM rpc_calls WHERE request_corr = ? ORDER BY position ASC`,
      )
      .all(requestCorr) as Record<string, unknown>[];
    return rows.map(mapCall);
  }

  getRequest(requestCorr: string): RequestRecord | null {
    const row = this.db
      .prepare(`SELECT * FROM rpc_requests WHERE request_corr = ?`)
      .get(requestCorr) as Record<string, unknown> | undefined;
    return row ? mapRequest(row) : null;
  }

  listRecentRequests(limit = 50): RequestRecord[] {
    const rows = this.db
      .prepare(
        `SELECT * FROM rpc_requests ORDER BY received_at DESC LIMIT ?`,
      )
      .all(Math.min(Math.max(limit, 1), 500)) as Record<string, unknown>[];
    return rows.map(mapRequest);
  }

  close(): void {
    this.db.close();
  }
}

export function openStore(dbPath: string): SqliteLedgerStore {
  return new SqliteLedgerStore(dbPath);
}
