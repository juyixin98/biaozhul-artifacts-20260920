/**
 * Per-call executor.
 *
 * One batch entry goes in; its ledger call row and (for side effects)
 * operation rows are maintained and exactly one SettledCall comes out:
 *  - a response object for requests, attributed to the entry's OWN position
 *  - null response for notifications — including recorded notification
 *    failures, which never become a response
 *
 * The independent operation number and the explicit idempotency key (never
 * the RPC id) are handled here; the orchestrator only schedules entries.
 */

import {
  METHOD_NOT_FOUND,
  NotFoundError,
  RpcException,
  STABLE_MESSAGES,
} from "../protocol/errors.js";
import type { RpcRequest } from "../protocol/types.js";
import { isNotification } from "../protocol/types.js";
import type {
  InvalidMessage,
  ValidMessage,
} from "../protocol/parser.js";
import type { OperationStatus } from "../state/store.js";
import { serializeOperation } from "../state/serialize.js";
import { encodeRpcId, newCallCorr } from "./ids.js";
import { KERNEL_RESOLVED_METHODS, type MethodContext, type MethodDefinition } from "./methods.js";
import type { KernelRuntime, SettledCall } from "./types.js";
import {
  InterruptedError,
  normalizeError,
  raceAbort,
  safeRedactedJson,
  withOperationEnvelope,
  type SideEffectOutcome,
} from "./support.js";

export interface ExecutorResult {
  readonly value: unknown;
  /** Async methods settle via a background continuation; no call finish here. */
  readonly asyncSettledExternally: boolean;
}

export async function runInvalidEntry(
  rt: KernelRuntime,
  requestCorr: string,
  entry: InvalidMessage,
): Promise<SettledCall> {
  const callCorr = newCallCorr();
  const nowIso = rt.clock().toISOString();
  rt.store.beginCall({
    callCorr,
    requestCorr,
    position: entry.position,
    rpcIdJson: null,
    isNotification: false,
    method: null,
    startedAt: nowIso,
  });
  rt.store.finishCall({
    callCorr,
    finishedAt: nowIso,
    status: "invalid",
    errorCode: entry.code,
    errorCategory: entry.category,
    errorMessage: entry.message,
  });
  rt.logger.rejected({
    requestCorr,
    callCorr,
    position: entry.position,
    decision: "rejected:contract",
    category: entry.category,
  });
  return {
    position: entry.position,
    response: {
      jsonrpc: "2.0",
      error: {
        code: entry.code,
        message: entry.message,
        data: { category: entry.category, correlationId: callCorr },
      },
      id: null,
    },
  };
}

export async function runValidEntry(
  rt: KernelRuntime,
  requestCorr: string,
  entry: ValidMessage,
  signal: AbortSignal | undefined,
): Promise<SettledCall> {
  const { request, position } = entry;
  const notification = isNotification(request);
  const callCorr = newCallCorr();
  const rpcIdJson = encodeRpcId(request.id);
  rt.store.beginCall({
    callCorr,
    requestCorr,
    position,
    rpcIdJson,
    isNotification: notification,
    method: request.method,
    startedAt: rt.clock().toISOString(),
  });

  try {
    const result = await execute(
      rt,
      { requestCorr, callCorr, rpcIdJson, request, notification, signal },
    );

    if (notification) {
      if (result.asyncSettledExternally) {
        // The background continuation finalizes the call row.
        return { position, response: null };
      }
      rt.store.finishCall({
        callCorr,
        finishedAt: rt.clock().toISOString(),
        status: "success",
      });
      rt.logger.accepted({
        requestCorr,
        callCorr,
        method: request.method,
        position,
        decision: "accepted:notification",
      });
      return { position, response: null };
    }

    rt.store.finishCall({
      callCorr,
      finishedAt: rt.clock().toISOString(),
      status: "success",
    });
    rt.logger.accepted({
      requestCorr,
      callCorr,
      method: request.method,
      position,
      decision: "accepted",
    });
    return {
      position,
      response: { jsonrpc: "2.0", result: result.value, id: request.id ?? null },
    };
  } catch (error) {
    if (signal?.aborted) {
      // Leave the call pending: interruptPending() terminal-marks it so a
      // late continuation can never overwrite the interrupted row.
      return { position, response: null };
    }
    const rpcError = normalizeError(error);
    // Notification failures are recorded but NEVER turned into a response.
    rt.store.finishCall({
      callCorr,
      finishedAt: rt.clock().toISOString(),
      status: "error",
      errorCode: rpcError.code,
      errorCategory: rpcError.category,
      errorMessage: rpcError.message,
    });
    rt.logger.rejected({
      requestCorr,
      callCorr,
      method: request.method,
      position,
      decision: notification
        ? "notification_failed_recorded"
        : "rejected:execution",
      category: rpcError.category,
      detail: { notification, code: rpcError.code },
    });
    if (notification) return { position, response: null };
    return {
      position,
      response: {
        jsonrpc: "2.0",
        error: {
          code: rpcError.code,
          message: rpcError.message,
          data: {
            category: rpcError.category,
            correlationId: callCorr,
            ...(rpcError.detail !== undefined ? { detail: rpcError.detail } : {}),
          },
        },
        id: request.id ?? null,
      },
    };
  }
}

interface ExecuteInput {
  readonly requestCorr: string;
  readonly callCorr: string;
  readonly rpcIdJson: string | null;
  readonly request: RpcRequest;
  readonly notification: boolean;
  readonly signal: AbortSignal | undefined;
}

async function execute(
  rt: KernelRuntime,
  input: ExecuteInput,
): Promise<ExecutorResult> {
  const { request } = input;
  const method = rt.registry.get(request.method);
  if (!method) {
    throw new RpcException(
      METHOD_NOT_FOUND,
      `${STABLE_MESSAGES.methodNotFound}: ${request.method}`,
      "method_not_found",
      { method: request.method },
    );
  }

  // validate() can only raise INVALID_PARAMS and runs before any op number
  // is allocated, so rejected calls leave no side-effect row.
  const validated = method.validate(request.params);

  if (KERNEL_RESOLVED_METHODS.has(method.name)) {
    return {
      value: resolveKernelMethod(rt, method.name, validated),
      asyncSettledExternally: false,
    };
  }

  if (!method.sideEffect) {
    const value = await raceAbort(
      Promise.resolve(method.execute(validated, methodContext(rt))),
      input.signal,
    );
    return { value, asyncSettledExternally: false };
  }

  if (method.mode === "async") {
    return executeAsyncSideEffect(rt, {
      ...input,
      method,
      validated,
    });
  }

  const value = await executeSyncSideEffect(rt, {
    ...input,
    method,
    validated,
  });
  return { value, asyncSettledExternally: false };
}

function methodContext(rt: KernelRuntime): MethodContext {
  return { now: rt.clock(), accounts: rt.accounts, vault: rt.vault };
}

function resolveKernelMethod(
  rt: KernelRuntime,
  name: string,
  params: Record<string, unknown>,
): unknown {
  if (name === "operations.get") {
    const opSeq = params.opSeq as number;
    const op = rt.store.getOperation(opSeq);
    if (!op) throw new NotFoundError("operation not found", { opSeq });
    return serializeOperation(op);
  }
  return {
    operations: rt.store
      .listOperations({
        status: params.status as OperationStatus | undefined,
        kind: params.kind as string | undefined,
        limit: params.limit as number | undefined,
      })
      .map(serializeOperation),
  };
}

interface SideEffectInput extends ExecuteInput {
  readonly method: MethodDefinition;
  readonly validated: Record<string, unknown>;
}

async function executeSyncSideEffect(
  rt: KernelRuntime,
  input: SideEffectInput,
): Promise<unknown> {
  const spec = input.method.sideEffect!;
  const idemKey = spec.idempotencyKey
    ? spec.idempotencyKey(input.validated)
    : null;

  const runOnce = async (): Promise<SideEffectOutcome> => {
    const opSeq = rt.store.beginOperation({
      callCorr: input.callCorr,
      requestCorr: input.requestCorr,
      rpcIdJson: input.rpcIdJson,
      idempotencyKey: idemKey,
      kind: spec.kind,
      startedAt: rt.clock().toISOString(),
      redactedInput: safeRedactedJson(spec, input.validated),
    });
    try {
      const result = await raceAbort(
        Promise.resolve(input.method.execute(input.validated, methodContext(rt))),
        input.signal,
      );
      rt.store.finishOperation({
        opSeq,
        finishedAt: rt.clock().toISOString(),
        status: "succeeded",
        resultJson: JSON.stringify(result),
      });
      return { opSeq, result, replayed: false };
    } catch (error) {
      if (input.signal?.aborted || error instanceof InterruptedError) {
        // Keep the operation pending; interruptPending() terminal-marks it.
        throw error;
      }
      const rpcError = normalizeError(error);
      rt.store.finishOperation({
        opSeq,
        finishedAt: rt.clock().toISOString(),
        status: "failed",
        errorCode: rpcError.code,
        errorCategory: rpcError.category,
        errorMessage: rpcError.message,
      });
      throw error;
    }
  };

  if (!idemKey) {
    return withOperationEnvelope(await runOnce());
  }

  // Explicit idempotency key: serialize same-key attempts process-wide. A
  // second successful call replays the original result and allocates NO new
  // operation number. The RPC id is never consulted here.
  const outcome = await withIdempotencyLock(rt, spec.kind, idemKey, async () => {
    const existing = rt.store.findOperationByIdempotencyKey(idemKey, spec.kind);
    if (existing) {
      return {
        opSeq: existing.opSeq,
        result: existing.resultJson ? JSON.parse(existing.resultJson) : null,
        replayed: true,
      } satisfies SideEffectOutcome;
    }
    return runOnce();
  });
  return withOperationEnvelope(outcome);
}

function executeAsyncSideEffect(
  rt: KernelRuntime,
  input: SideEffectInput,
): ExecutorResult {
  const spec = input.method.sideEffect!;
  const opSeq = rt.store.beginOperation({
    callCorr: input.callCorr,
    requestCorr: input.requestCorr,
    rpcIdJson: input.rpcIdJson,
    idempotencyKey: null,
    kind: spec.kind,
    startedAt: rt.clock().toISOString(),
    redactedInput: safeRedactedJson(spec, input.validated),
  });

  const continuation = (async () => {
    try {
      const result = await Promise.resolve(
        input.method.execute(input.validated, methodContext(rt)),
      );
      rt.store.finishOperation({
        opSeq,
        finishedAt: rt.clock().toISOString(),
        status: "succeeded",
        resultJson: JSON.stringify(result),
      });
      if (input.notification) {
        rt.store.finishCall({
          callCorr: input.callCorr,
          finishedAt: rt.clock().toISOString(),
          status: "success",
        });
      }
    } catch (error) {
      const rpcError = normalizeError(error);
      rt.store.finishOperation({
        opSeq,
        finishedAt: rt.clock().toISOString(),
        status: "failed",
        errorCode: rpcError.code,
        errorCategory: rpcError.category,
        errorMessage: rpcError.message,
      });
      if (input.notification) {
        rt.store.finishCall({
          callCorr: input.callCorr,
          finishedAt: rt.clock().toISOString(),
          status: "error",
          errorCode: rpcError.code,
          errorCategory: rpcError.category,
          errorMessage: rpcError.message,
        });
      }
    }
  })();

  rt.trackBackground(continuation);

  // Immediate acceptance; the terminal state is observable via operations.get.
  return {
    value: {
      accepted: true,
      opSeq,
      status: "pending",
      poll: "operations.get",
    },
    asyncSettledExternally: true,
  };
}

async function withIdempotencyLock<T>(
  rt: KernelRuntime,
  kind: string,
  key: string,
  work: () => Promise<T>,
): Promise<T> {
  const composite = `${kind} ${key}`;
  // Wait for any earlier same-key attempt (successful or not) to settle.
  while (rt.idemLocks.has(composite)) {
    try {
      await rt.idemLocks.get(composite);
    } catch {
      // A failed in-flight attempt must release waiters, not trap them.
    }
  }
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  rt.idemLocks.set(composite, gate);
  try {
    return await work();
  } finally {
    rt.idemLocks.delete(composite);
    release();
  }
}
