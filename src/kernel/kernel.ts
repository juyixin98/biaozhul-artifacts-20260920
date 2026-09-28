import { ErrorCode, type JsonValue, type ParsedMessage, type RpcResponse } from '../contract/protocol.js';
import { isEmptyBatch, parseEnvelope } from '../contract/parse.js';
import { redact } from '../diagnostics/redact.js';
import type { DiagnosticLogger } from '../diagnostics/logger.js';
import type { StateStore } from '../state/store.js';
import { IdempotencyInsertConflict } from '../state/store.js';
import { duplicateKey, newBatchId, newOperationId, renderId } from './ids.js';
import type { HandlerContext, MethodDefinition } from './method.js';
import { isDetachedWork, MethodError } from './method.js';
import { TaskRegistry } from './task-registry.js';

/**
 * Execution kernel. Transport-agnostic: given a raw request body and a
 * connection signal it produces an HTTP-ish result. The Fastify layer is a
 * thin adapter over this.
 *
 * Guarantees implemented here (see README and tests for each):
 *  - Notifications produce no response slot, but their failures are persisted
 *    as failed operations / diagnostic events.
 *  - Batch responses stay aligned to INPUT POSITIONS; concurrent/out-of-order
 *    completion can never cross-wire results.
 *  - A duplicate RPC id inside one batch is explicitly rejected (-32001) for
 *    every occurrence after the first; identical ids across batches never
 *    collide, because ids are never used as storage keys.
 *  - Parse errors (-32700) are returned before any envelope semantics; invalid
 *    requests/elements are -32600; method/business failures are their own
 *    codes. The layers never blur into each other.
 *  - Side effects get an independent operationId and an OPTIONAL client
 *    idempotency key scoped per method; the RPC id is never that key.
 */

export interface KernelRequestContext {
  connectionId: string;
  /** Aborts when the transport detects the client went away mid-request. */
  signal: AbortSignal;
}

export interface KernelHttpResult {
  status: number;
  /** null means "no JSON-RPC body" — used only when the whole batch is notifications. */
  payload: JsonValue | RpcResponse | RpcResponse[] | null;
  batchId: string | null;
}

export interface KernelDeps {
  store: StateStore;
  methods: Map<string, MethodDefinition>;
  tasks: TaskRegistry;
  logger: DiagnosticLogger;
  now?: () => number;
}

export const MAX_BATCH_ELEMENTS = 10_000;

export class Kernel {
  private readonly now: () => number;

  constructor(private readonly deps: KernelDeps) {
    this.now = deps.now ?? Date.now;
  }

  async handle(rawBody: string, ctx: KernelRequestContext): Promise<KernelHttpResult> {
    const parsed = parseEnvelope(rawBody);
    if (!parsed.ok) {
      // Layer 1: the bytes are not JSON. Nothing else can be attempted.
      // Persist only the byte length and an oversized flag — never a raw
      // excerpt, which could contain secret-bearing string values.
      this.deps.logger.indeterminate('parse-error', 'body-is-not-json', {
        connectionId: ctx.connectionId,
        rpcId: null,
      }, { rawByteLength: parsed.rawByteLength, oversized: parsed.oversized });
      // Use fixed messages: engines embed a source excerpt in JSON.parse
      // errors, which itself could leak a secret back into logs/responses.
      return {
        status: 200,
        batchId: null,
        payload: {
          jsonrpc: '2.0',
          error: {
            code: ErrorCode.PARSE_ERROR,
            message: parsed.oversized
              ? 'Parse error: request body exceeds 1 MiB limit'
              : 'Parse error: body is not valid JSON',
          },
          id: null,
        },
      };
    }

    if (parsed.topLevel === 'batch' && isEmptyBatch(parsed.messages)) {
      this.deps.logger.rejected('batch-empty', 'empty-array-is-invalid-request', {
        connectionId: ctx.connectionId,
      });
      return {
        status: 200,
        batchId: null,
        payload: {
          jsonrpc: '2.0',
          error: {
            code: ErrorCode.INVALID_REQUEST,
            message: 'Invalid Request: an empty batch array is not a valid JSON-RPC Request',
          },
          id: null,
        },
      };
    }

    if (parsed.topLevel === 'batch' && parsed.messages.length > MAX_BATCH_ELEMENTS) {
      // Resource bound: refuse an oversized fan-out rather than spawning tens
      // of thousands of concurrent slots/timers. A single envelope error.
      this.deps.logger.rejected('batch-too-large', 'batch-element-count-exceeds-limit', {
        connectionId: ctx.connectionId,
      }, { elementCount: parsed.messages.length, limit: MAX_BATCH_ELEMENTS });
      return {
        status: 200,
        batchId: null,
        payload: {
          jsonrpc: '2.0',
          error: {
            code: ErrorCode.INVALID_REQUEST,
            message: `Invalid Request: batch exceeds ${MAX_BATCH_ELEMENTS} elements`,
            data: { limit: MAX_BATCH_ELEMENTS, count: parsed.messages.length },
          },
          id: null,
        },
      };
    }

    const batchId = parsed.topLevel === 'batch' ? newBatchId(this.now) : null;
    this.deps.logger.accepted(
      parsed.topLevel === 'batch' ? 'batch-received' : 'request-received',
      'envelope-valid',
      { connectionId: ctx.connectionId, batchId },
      { elementCount: parsed.messages.length },
    );

    // Duplicate ids are detected up front, structurally, never by execution
    // side effects.
    const seenIds = new Set<string>();
    const duplicatePositions = new Set<number>();
    parsed.messages.forEach((message, index) => {
      if (message.kind === 'request') {
        const key = duplicateKey(message.id);
        if (seenIds.has(key)) duplicatePositions.add(index);
        else seenIds.add(key);
      }
    });

    // Execute concurrently but assign each result to its fixed input slot,
    // so out-of-order completion never reorders responses.
    const slots: (RpcResponse | null)[] = await Promise.all(
      parsed.messages.map((message, index) =>
        this.executeSlot(message, index, {
          ...ctx,
          batchId,
          isDuplicateId: duplicatePositions.has(index),
        }),
      ),
    );

    if (parsed.topLevel === 'single') {
      const only = slots[0] ?? null;
      // A single notification is the only single-message case with no slot.
      if (only === null) return { status: 204, batchId, payload: null };
      return { status: 200, batchId, payload: only };
    }

    const responses = slots.filter((slot): slot is RpcResponse => slot !== null);
    // All-notification batches: HTTP 204 semantics expressed as "no body".
    if (responses.length === 0) {
      return { status: 204, batchId, payload: null };
    }
    return { status: 200, batchId, payload: responses };
  }

  private async executeSlot(
    message: ParsedMessage,
    position: number,
    meta: { connectionId: string; signal: AbortSignal; batchId: string | null; isDuplicateId: boolean },
  ): Promise<RpcResponse | null> {
    const ids = {
      connectionId: meta.connectionId,
      batchId: meta.batchId,
      rpcId: message.kind === 'request' ? renderId(message.id) : null,
    };

    if (message.kind === 'invalid') {
      this.deps.logger.rejected(
        'invalid-element',
        message.reason === 'invalid-batch-element' ? 'batch-element-not-a-request' : 'not-a-request-object',
        ids,
        { position, message: message.message },
      );
      return {
        jsonrpc: '2.0',
        error: {
          code: ErrorCode.INVALID_REQUEST,
          message: message.message,
          data: { position, reason: message.reason },
        },
        id: message.id,
      };
    }

    if (meta.isDuplicateId && message.kind === 'request') {
      // Explicit duplicate policy: reject every occurrence after the first.
      // The position-aligned response array keeps attribution deterministic.
      this.deps.logger.rejected('duplicate-id', 'duplicate-rpc-id-within-batch', ids, { position });
      return {
        jsonrpc: '2.0',
        error: {
          code: ErrorCode.SERVER_ERROR_DUPLICATE_ID,
          message: 'Invalid Request: duplicate id within the same batch',
          data: { position },
        },
        id: message.id,
      };
    }

    const isNotification = message.kind === 'notification';
    const responseId = message.kind === 'request' ? message.id : null;

    let result: MessageOutcome;
    try {
      result = await this.executeMessage(message.request, {
        connectionId: meta.connectionId,
        batchId: meta.batchId,
        signal: meta.signal,
        rpcId: ids.rpcId,
        isNotification,
        position,
      });
    } catch (err) {
      // Infrastructure failure (e.g. the state adapter throwing), not a
      // handler MethodError: those are converted inside run*. Anything
      // escaping here must still become a well-formed envelope rather than
      // an HTTP 500, so one bad slot never corrupts the whole batch.
      this.deps.logger.indeterminate(
        'internal-error',
        'unexpected-error-outside-handler',
        ids,
        { method: message.request.method, error: describeError(err) },
      );
      if (isNotification) return null;
      return {
        jsonrpc: '2.0',
        error: {
          code: ErrorCode.INTERNAL_ERROR,
          message: 'Internal error',
        },
        id: responseId,
      };
    }

    if (isNotification) return null; // notifications NEVER produce a response slot
    if (result.kind === 'error') {
      return { jsonrpc: '2.0', error: result.error, id: responseId };
    }
    return { jsonrpc: '2.0', result: result.value, id: responseId };
  }

  private async executeMessage(
    request: { method: string; params?: JsonValue | undefined },
    ctx: {
      connectionId: string;
      batchId: string | null;
      signal: AbortSignal;
      rpcId: string | null;
      isNotification: boolean;
      position: number;
    },
  ): Promise<{ kind: 'ok'; value: JsonValue } | { kind: 'error'; error: { code: number; message: string; data?: JsonValue } }> {
    const def = this.deps.methods.get(request.method);
    if (!def) {
      this.deps.logger.rejected('method-not-found', 'no-such-method', {
        connectionId: ctx.connectionId,
        batchId: ctx.batchId,
        rpcId: ctx.rpcId,
      }, { method: request.method });
      return {
        kind: 'error',
        error: { code: ErrorCode.METHOD_NOT_FOUND, message: `Method not found: ${request.method}` },
      };
    }

    if (!def.sideEffect) {
      return this.runReadOnly(def, request.params, ctx);
    }
    return this.runSideEffect(def, request.params, ctx);
  }

  private async runReadOnly(
    def: MethodDefinition,
    params: JsonValue | undefined,
    ctx: ExecutionCtx,
  ): Promise<MessageOutcome> {
    const handlerCtx = this.handlerContext(null, ctx.signal, () => false);
    try {
      const value = await def.handle(params, handlerCtx);
      // Read-only methods must never declare detached work.
      if (isDetachedWork(value)) {
        throw new Error(`read-only method ${def.method} returned detached work`);
      }
      return { kind: 'ok', value };
    } catch (err) {
      return this.toErrorOutcome(err, ctx, def.method, null);
    }
  }

  private async runSideEffect(
    def: MethodDefinition,
    params: JsonValue | undefined,
    ctx: ExecutionCtx,
  ): Promise<MessageOutcome> {
    const idemKey = def.idempotencyKeyOf ? def.idempotencyKeyOf(params) : null;
    const fingerprint = def.fingerprint ? def.fingerprint(params) : null;

    // Every side-effect attempt gets its OWN operation id up front, including
    // attempts that will be rejected by idempotency rules. The RPC id is
    // never used here.
    const operationId = newOperationId(this.now);
    const opIds = {
      connectionId: ctx.connectionId,
      batchId: ctx.batchId,
      rpcId: ctx.rpcId,
      operationId,
    };

    if (idemKey !== null) {
      const decision = await this.resolveIdempotency(
        def.method,
        idemKey,
        fingerprint,
        operationId,
        ctx,
      );
      if (decision) return decision;
    }

    try {
      this.deps.store.startOperation({
        operationId,
        batchId: ctx.batchId,
        connectionId: ctx.connectionId,
        method: def.method,
        idempotencyKey: idemKey,
        fingerprint,
        redactedRequest: redact(params) ?? null,
        startedAt: this.now(),
      });
    } catch (err) {
      if (err instanceof IdempotencyInsertConflict) {
        // Concurrent first-execution race for the same (method,key): the
        // winner's row now exists, so re-run resolution. This attempt's own
        // operation row is recorded there as a rejection audit entry.
        const decision = await this.resolveIdempotency(
          def.method,
          // The unique constraint only exists for a non-null key.
          idemKey as string,
          fingerprint,
          operationId,
          ctx,
        );
        if (decision) return decision;
      }
      throw err;
    }

    this.deps.logger.accepted(
      ctx.isNotification ? 'notification-started' : 'operation-started',
      idemKey ? 'side-effect-with-idempotency-key' : 'side-effect-no-key',
      opIds,
      { method: def.method, position: ctx.position },
    );

    // Every side effect gets its OWN controller. While the call is attached
    // to the request, a lost connection aborts it; the moment a handler
    // detaches background work we unlink it, so async tasks survive the
    // connection closing and are only stopped via tasks.cancel.
    const taskController = new AbortController();
    const onConnectionAbort = (): void => {
      taskController.abort(
        ctx.signal.reason instanceof Error ? ctx.signal.reason : new Error('client disconnected'),
      );
    };
    if (ctx.signal.aborted) onConnectionAbort();
    else ctx.signal.addEventListener('abort', onConnectionAbort, { once: true });

    const handlerCtx = this.handlerContext(operationId, taskController.signal, (target, reason) =>
      this.requestCancel(target, reason),
    );

    let returned: unknown;
    try {
      returned = await def.handle(params, handlerCtx);
    } catch (err) {
      ctx.signal.removeEventListener('abort', onConnectionAbort);
      if (ctx.signal.aborted) {
        // Connection lost mid-execution: record once, under the abort class,
        // and return the honest outcome (which the transport may not deliver).
        this.deps.store.finishOperation(operationId, {
          status: 'failed',
          errorCode: ErrorCode.SERVER_ERROR_ABORTED,
          errorMessage: 'Execution aborted: client connection was lost',
          finishedAt: this.now(),
        });
        this.deps.logger.indeterminate('request-aborted', 'connection-lost-during-execution', opIds, {
          method: def.method,
          error: describeError(err),
        });
        return {
          kind: 'error',
          error: {
            code: ErrorCode.SERVER_ERROR_ABORTED,
            message: 'Execution aborted: client connection was lost',
          },
        };
      }
      this.finishFailed(operationId, err);
      this.deps.logger.rejected(
        ctx.isNotification ? 'notification-failed' : 'operation-failed',
        err instanceof MethodError ? 'method-error' : 'unexpected-error',
        opIds,
        { error: describeError(err) },
      );
      return this.toErrorOutcome(err, ctx, def.method, operationId, { alreadyFinished: true });
    }

    if (isDetachedWork(returned)) {
      // Background work outlives the HTTP connection.
      ctx.signal.removeEventListener('abort', onConnectionAbort);
      this.registerDetached(operationId, returned.work, taskController, def.method, ctx);
      return { kind: 'ok', value: returned.reply };
    }

    // Synchronous side effect completed while the client may have gone away;
    // record the honest outcome regardless.
    ctx.signal.removeEventListener('abort', onConnectionAbort);
    try {
      this.deps.store.finishOperation(operationId, {
        status: 'succeeded',
        result: returned as JsonValue,
        finishedAt: this.now(),
      });
      this.deps.logger.accepted(
        ctx.isNotification ? 'notification-succeeded' : 'operation-succeeded',
        'side-effect-completed',
        opIds,
      );
      return { kind: 'ok', value: returned as JsonValue };
    } catch (err) {
      return this.toErrorOutcome(err, ctx, def.method, operationId);
    }
  }

  /**
   * Resolve an idempotency-keyed attempt against any existing operation.
   *
   * Returns a terminal outcome when a prior operation exists:
   *  - succeeded  -> replay stored result, NO new operation row
   *  - failed/cancelled -> replay recorded failure, NO new operation row
   *  - pending    -> reject as conflict; this ATTEMPT still gets its own
   *                  failed audit row (operationId allocated by the caller)
   *  - fingerprint mismatch -> reject as conflict, same audit-row treatment
   *
   * Returns null when no prior operation exists: the caller proceeds to
   * start the real side effect.
   */
  private async resolveIdempotency(
    method: string,
    key: string,
    fingerprint: string | null,
    attemptOperationId: string,
    ctx: ExecutionCtx,
  ): Promise<MessageOutcome | null> {
    const existing = this.deps.store.findByIdempotencyKey(method, key);
    if (!existing) return null;

    const ids = {
      connectionId: ctx.connectionId,
      batchId: ctx.batchId,
      rpcId: ctx.rpcId,
      operationId: attemptOperationId,
    };

    const mismatched =
      fingerprint !== null && existing.fingerprint !== null && fingerprint !== existing.fingerprint;

    if (mismatched || existing.status === 'pending') {
      const reason = mismatched ? 'same-key-different-payload' : 'matching-operation-still-pending';
      const code = ErrorCode.SERVER_ERROR_IDEMPOTENCY_CONFLICT;
      const message = mismatched
        ? `Idempotency key "${key}" was already used with different parameters`
        : `An operation with idempotency key "${key}" is already in progress`;

      // Persist this rejected attempt as its OWN failed operation, but with a
      // null idempotency key so it never collides with the guarded first run,
      // and a pointer to the operation it conflicts with.
      this.recordRejectedAttempt(attemptOperationId, method, fingerprint, existing.operationId, code, message, ctx);
      this.deps.logger.rejected('idempotency-conflict', reason, ids, {
        method,
        existingOperationId: existing.operationId,
      });
      return {
        kind: 'error',
        error: {
          code,
          message,
          data: {
            existingOperationId: existing.operationId,
            reason: mismatched ? 'fingerprint-mismatch' : 'in-progress',
            attemptOperationId,
          },
        },
      };
    }

    if (existing.status === 'succeeded') {
      this.deps.logger.accepted('idempotent-replay', 'replaying-stored-result', {
        ...ids,
        operationId: existing.operationId,
      }, { method });
      return { kind: 'ok', value: existing.result ?? null };
    }

    // failed / cancelled: replay the recorded terminal failure so a retry
    // cannot trigger a second side effect; the client sees the original class.
    this.deps.logger.accepted('idempotent-replay', 'replaying-recorded-failure', {
      ...ids,
      operationId: existing.operationId,
    }, { method, status: existing.status });
    return {
      kind: 'error',
      error: {
        code: existing.errorCode ?? ErrorCode.SERVER_ERROR_METHOD_FAILED,
        message: existing.errorMessage ?? 'Recorded operation failure',
        data: { replayed: true, originalOperationId: existing.operationId },
      },
    };
  }

  private recordRejectedAttempt(
    attemptOperationId: string,
    method: string,
    fingerprint: string | null,
    existingOperationId: string,
    code: number,
    message: string,
    ctx: ExecutionCtx,
  ): void {
    this.deps.store.startOperation({
      operationId: attemptOperationId,
      batchId: ctx.batchId,
      connectionId: ctx.connectionId,
      method,
      // Null key: this audit row must not occupy the (method,key) slot.
      idempotencyKey: null,
      fingerprint,
      replayedFromOperationId: existingOperationId,
      redactedRequest: null,
      startedAt: this.now(),
    });
    this.deps.store.finishOperation(attemptOperationId, {
      status: 'failed',
      errorCode: code,
      errorMessage: message,
      finishedAt: this.now(),
    });
  }

  private registerDetached(
    operationId: string,
    work: Promise<JsonValue>,
    controller: AbortController,
    method: string,
    ctx: ExecutionCtx,
  ): void {
    const ids = {
      connectionId: ctx.connectionId,
      batchId: ctx.batchId,
      rpcId: ctx.rpcId,
      operationId,
    };
    this.deps.tasks.register(
      operationId,
      work.then(
        (value) => {
          this.deps.store.finishOperation(operationId, {
            status: 'succeeded',
            result: value,
            finishedAt: this.now(),
          });
          this.deps.logger.accepted('detached-task-succeeded', 'background-work-completed', ids);
          return value;
        },
        (err: unknown) => {
          const cancelled = controller.signal.aborted;
          if (cancelled) {
            this.deps.store.finishOperation(operationId, {
              status: 'cancelled',
              errorCode: ErrorCode.SERVER_ERROR_CANCELLED,
              errorMessage: abortReason(controller) ?? 'Task cancelled',
              finishedAt: this.now(),
            });
            this.deps.logger.rejected('detached-task-cancelled', 'cancelled-via-tasks-cancel', ids, {
              error: describeError(err),
            });
          } else {
            this.finishFailed(operationId, err);
            this.deps.logger.rejected('detached-task-failed', 'background-work-threw', ids, {
              error: describeError(err),
            });
          }
          // Swallow after recording: nothing else is awaiting this promise.
          return undefined as unknown as JsonValue;
        },
      ),
      controller,
      this.now(),
    );
    this.deps.logger.accepted('detached-task-registered', 'reply-sent-work-continues', ids, {
      method,
    });
  }

  private requestCancel(targetOperationId: string, reason: string): boolean {
    const task = this.deps.tasks.get(targetOperationId);
    if (!task) return false;
    task.cancel(reason);
    this.deps.logger.indeterminate('cancel-requested', 'client-requested-cancellation', {
      operationId: targetOperationId,
    }, { reason });
    return true;
  }

  private finishFailed(operationId: string, err: unknown): void {
    const code = err instanceof MethodError ? err.code : ErrorCode.INTERNAL_ERROR;
    this.deps.store.finishOperation(operationId, {
      status: 'failed',
      errorCode: code,
      errorMessage: err instanceof Error ? err.message : String(err),
      finishedAt: this.now(),
    });
  }

  private toErrorOutcome(
    err: unknown,
    ctx: ExecutionCtx,
    method: string,
    operationId: string | null,
    opts: { alreadyFinished?: boolean } = {},
  ): MessageOutcome {
    const ids = {
      connectionId: ctx.connectionId,
      batchId: ctx.batchId,
      rpcId: ctx.rpcId,
      operationId,
    };
    const aborted = ctx.signal.aborted;
    if (aborted) {
      // Connection lost before the synchronous call settled. Recorded; the
      // transport may be unable to deliver this response.
      if (operationId && !opts.alreadyFinished) this.finishFailed(operationId, err);
      this.deps.logger.indeterminate('request-aborted', 'connection-lost-during-execution', ids, {
        method,
        error: describeError(err),
      });
      return {
        kind: 'error',
        error: {
          code: ErrorCode.SERVER_ERROR_ABORTED,
          message: 'Execution aborted: client connection was lost',
        },
      };
    }
    if (operationId && !opts.alreadyFinished) this.finishFailed(operationId, err);
    this.deps.logger.rejected(
      ctx.isNotification ? 'notification-failed' : 'method-failed',
      err instanceof MethodError ? 'method-error' : 'unexpected-error',
      ids,
      { method, error: describeError(err) },
    );
    if (err instanceof MethodError) {
      return {
        kind: 'error',
        error: { code: err.code, message: err.message, ...(err.data !== undefined ? { data: err.data } : {}) },
      };
    }
    // Non-MethodError failures are infrastructure faults. The detailed message
    // is persisted server-side (error_message / diagnostic event) for local
    // debugging, but the wire response stays generic so driver internals
    // (paths, SQL text) are never reflected to the caller.
    return {
      kind: 'error',
      error: {
        code: ErrorCode.INTERNAL_ERROR,
        message: 'Internal error',
      },
    };
  }

  private handlerContext(
    operationId: string | null,
    signal: AbortSignal,
    requestCancel: (target: string, reason: string) => boolean,
  ): HandlerContext {
    return {
      operationId,
      signal,
      requestCancel,
      sleep: (ms: number) => abortableSleep(ms, signal),
    };
  }
}

type ExecutionCtx = {
  connectionId: string;
  batchId: string | null;
  signal: AbortSignal;
  rpcId: string | null;
  isNotification: boolean;
  position: number;
};

type MessageOutcome =
  | { kind: 'ok'; value: JsonValue }
  | { kind: 'error'; error: { code: number; message: string; data?: JsonValue } };

function abortReason(controller: AbortController): string | null {
  const reason = controller.signal.reason;
  return reason instanceof Error ? reason.message : typeof reason === 'string' ? reason : null;
}

function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(abortError(signal));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(abortError(signal));
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

function abortError(signal: AbortSignal): Error {
  const reason = signal.reason;
  if (reason instanceof Error) return reason;
  return new Error(typeof reason === 'string' ? reason : 'aborted');
}

function describeError(err: unknown): JsonValue {
  if (err instanceof MethodError) {
    return { name: err.name, code: err.code, message: err.message };
  }
  if (err instanceof Error) return { name: err.name, message: err.message };
  return String(err);
}
