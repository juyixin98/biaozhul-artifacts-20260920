/**
 * Read-only diagnostic views over the ledger.
 *
 * Every view carries a human- and machine-readable `decision` explaining why
 * the request/call/operation was accepted, rejected, or left undecidable,
 * plus the correlation ids needed to jump between levels. Only redacted
 * columns are returned.
 */

import type {
  CallRecord,
  LedgerStore,
  OperationRecord,
  RequestRecord,
} from "../state/store.js";
import { serializeOperation } from "../state/serialize.js";

export interface CallView {
  readonly callCorr: string;
  readonly position: number;
  readonly rpcId: unknown;
  readonly isNotification: boolean;
  readonly method: string | null;
  readonly status: string;
  readonly startedAt: string;
  readonly finishedAt: string | null;
  readonly error: {
    readonly code: number;
    readonly category: string;
    readonly message: string;
  } | null;
}

export interface RequestDetailView {
  readonly requestCorr: string;
  readonly receivedAt: string;
  readonly finishedAt: string | null;
  readonly payloadKind: string;
  readonly sizeBytes: number;
  readonly counts: {
    readonly calls: number;
    readonly notifications: number;
    readonly invalid: number;
  };
  readonly decision: {
    readonly verdict: string;
    readonly layer: string;
    readonly category: string | null;
    readonly rationale: string;
  };
  readonly calls: readonly CallView[];
  readonly operations: readonly unknown[];
}

export class DiagnosticsQueries {
  constructor(private readonly store: LedgerStore) {}

  explainRequest(record: RequestRecord): {
    verdict: string;
    layer: string;
    category: string | null;
    rationale: string;
  } {
    switch (record.verdict) {
      case "rejected":
        return {
          verdict: "rejected",
          layer: record.layer,
          category: record.failureCategory,
          rationale:
            record.reason ??
            `rejected at the ${record.layer} layer (${record.failureCategory ?? "unknown"})`,
        };
      case "undecidable":
        return {
          verdict: "undecidable",
          layer: record.layer,
          category: record.failureCategory,
          rationale:
            record.reason ??
            "transport ended before delivery; outcome cannot be determined by the server",
        };
      default:
        return {
          verdict: "accepted",
          layer: record.layer,
          category: null,
          rationale: `processed ${record.callCount} element(s): ${record.notificationCount} notification(s), ${record.invalidCount} invalid element(s)`,
        };
    }
  }

  getRequest(requestCorr: string): RequestDetailView | null {
    const record = this.store.getRequest(requestCorr);
    if (!record) return null;
    const calls = this.store.listCallsByRequest(requestCorr);
    const ops = this.store
      .listOperations({ limit: 500 })
      .filter((op) => op.requestCorr === requestCorr)
      .map(serializeOperation);
    return {
      requestCorr: record.requestCorr,
      receivedAt: record.receivedAt,
      finishedAt: record.finishedAt,
      payloadKind: record.payloadKind,
      sizeBytes: record.sizeBytes,
      counts: {
        calls: record.callCount,
        notifications: record.notificationCount,
        invalid: record.invalidCount,
      },
      decision: this.explainRequest(record),
      calls: calls.map(toCallView),
      operations: ops,
    };
  }

  listRequests(limit = 50): readonly unknown[] {
    return this.store.listRecentRequests(limit).map((record) => ({
      requestCorr: record.requestCorr,
      receivedAt: record.receivedAt,
      payloadKind: record.payloadKind,
      verdict: record.verdict,
      layer: record.layer,
      failureCategory: record.failureCategory,
      counts: {
        calls: record.callCount,
        notifications: record.notificationCount,
        invalid: record.invalidCount,
      },
      rationale: this.explainRequest(record).rationale,
    }));
  }

  listOperations(filter: {
    status?: string;
    kind?: string;
    limit?: number;
  }): readonly unknown[] {
    return this.store
      .listOperations({
        status: filter.status as OperationRecord["status"] | undefined,
        kind: filter.kind,
        limit: filter.limit,
      })
      .map(serializeOperation);
  }

  getOperation(opSeq: number): unknown | null {
    const op = this.store.getOperation(opSeq);
    return op ? serializeOperation(op) : null;
  }
}

function toCallView(call: CallRecord): CallView {
  return {
    callCorr: call.callCorr,
    position: call.position,
    rpcId: call.rpcIdJson === null ? null : safeParse(call.rpcIdJson),
    isNotification: call.isNotification,
    method: call.method,
    status: call.status,
    startedAt: call.startedAt,
    finishedAt: call.finishedAt,
    error:
      call.errorCode !== null
        ? {
            code: call.errorCode,
            category: call.errorCategory ?? "unknown",
            message: call.errorMessage ?? "",
          }
        : null,
  };
}

function safeParse(json: string): unknown {
  try {
    return JSON.parse(json);
  } catch {
    return json;
  }
}
