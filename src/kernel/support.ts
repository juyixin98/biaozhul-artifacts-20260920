/**
 * Execution support shared by the orchestrator and the per-call executor:
 * interruption signal racing, error normalization into the JSON-RPC failure
 * taxonomy, and operation-result envelope helpers.
 */

import {
  INTERNAL_ERROR,
  RpcException,
  STABLE_MESSAGES,
} from "../protocol/errors.js";
import type { FailureCategory } from "../protocol/types.js";
import type { MethodDefinition } from "./methods.js";

export class InterruptedError extends Error {
  constructor() {
    super("transport interrupted");
    this.name = "InterruptedError";
  }
}

export interface NormalizedError {
  readonly code: number;
  readonly category: FailureCategory;
  readonly message: string;
  readonly detail?: unknown;
}

export interface SideEffectOutcome {
  readonly opSeq: number;
  readonly result: unknown;
  readonly replayed: boolean;
}

export function normalizeError(error: unknown): NormalizedError {
  if (error instanceof InterruptedError) {
    return {
      code: -32603,
      category: "interrupted",
      message: "connection lost before response delivery",
    };
  }
  if (error instanceof RpcException) {
    return {
      code: error.code,
      category: error.category,
      message: error.message,
      ...(error.detail !== undefined ? { detail: error.detail } : {}),
    };
  }
  return {
    code: INTERNAL_ERROR,
    category: "internal",
    message: STABLE_MESSAGES.internalError,
  };
}

export function withOperationEnvelope(outcome: SideEffectOutcome): unknown {
  return {
    ...(outcome.result as Record<string, unknown>),
    operation: {
      opSeq: outcome.opSeq,
      replayed: outcome.replayed,
    },
  };
}

export function safeParseId(rpcIdJson: string): unknown {
  try {
    return JSON.parse(rpcIdJson);
  } catch {
    return rpcIdJson;
  }
}

export function safeRedactedJson(
  spec: NonNullable<MethodDefinition["sideEffect"]>,
  params: Record<string, unknown>,
): string {
  try {
    return JSON.stringify(spec.redactInput(params));
  } catch {
    return JSON.stringify({ redactionError: true });
  }
}

/**
 * Reject/abort early when the client disconnected while awaiting a handler.
 * The handler promise is intentionally NOT cancelled (Node cannot cancel
 * arbitrary promises); conditional ledger updates ensure its late result
 * cannot overwrite the interrupted terminal state.
 */
export async function raceAbort<T>(
  work: Promise<T>,
  signal: AbortSignal | undefined,
): Promise<T> {
  if (!signal) return work;
  if (signal.aborted) throw new InterruptedError();
  let onAbort: () => void = () => {};
  const abort = new Promise<never>((_, reject) => {
    onAbort = () => reject(new InterruptedError());
    signal.addEventListener("abort", onAbort, { once: true });
  });
  try {
    return await Promise.race([work, abort]);
  } finally {
    signal.removeEventListener("abort", onAbort);
  }
}
