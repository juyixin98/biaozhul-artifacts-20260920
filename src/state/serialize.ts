/**
 * Wire serialization of a stored OperationRecord.
 *
 * Lives at the state layer because both the execution kernel (results of
 * operations.get/list) and the diagnostics module project the same record;
 * neither needs to own the shape. Only redacted columns are surfaced.
 */

import type { OperationRecord } from "./store.js";

export function serializeOperation(op: OperationRecord): unknown {
  return {
    opSeq: op.opSeq,
    kind: op.kind,
    status: op.status,
    idempotencyKey: op.idempotencyKey,
    startedAt: op.startedAt,
    finishedAt: op.finishedAt,
    redactedInput: op.redactedInput ? JSON.parse(op.redactedInput) : null,
    result: op.resultJson ? JSON.parse(op.resultJson) : null,
    error: op.errorCode
      ? {
          code: op.errorCode,
          category: op.errorCategory,
          message: op.errorMessage,
        }
      : null,
    callCorr: op.callCorr,
    requestCorr: op.requestCorr,
  };
}
