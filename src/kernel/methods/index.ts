import { ErrorCode, type JsonValue } from '../../contract/protocol.js';
import type { DomainStore } from '../../state/domain-store.js';
import { DuplicateKeyError } from '../../state/domain-store.js';
import type { StateStore } from '../../state/store.js';
import type { TaskRegistry } from '../task-registry.js';
import type { HandlerContext, MethodDefinition } from '../method.js';
import { MethodError } from '../method.js';
import {
  booleanField,
  integerField,
  rejectUnknownFields,
  requireObject,
  stringField,
} from './validate.js';

/** Synthetic business failure code for tasks asked to fail. */
export const TASK_FAILED_CODE = -32050;

export interface BuildMethodsDeps {
  domain: DomainStore;
  store: StateStore;
  tasks: TaskRegistry;
  /** Upper bound on accepted task delay (validated against config). */
  maxTaskDelayMs: number;
  now?: () => number;
}

/**
 * Build the method table. Methods are pure fixtures over the local domain
 * store; there are no network or clock dependencies except injected ones.
 */
export function buildMethods(deps: BuildMethodsDeps): Map<string, MethodDefinition> {
  const now = deps.now ?? Date.now;
  const methods = new Map<string, MethodDefinition>();

  const define = (m: MethodDefinition): void => {
    methods.set(m.method, m);
  };

  define({
    method: 'ping',
    sideEffect: false,
    handle: () => 'pong',
  });

  define({
    method: 'echo',
    sideEffect: false,
    handle: (params) => params ?? null,
  });

  // Read-only delay. Stays attached to the request, so losing the connection
  // aborts it; used to verify the indeterminate abort diagnostics path.
  define({
    method: 'debug.sleep',
    sideEffect: false,
    handle: async (params, ctx) => {
      const obj = requireObject(params, 'debug.sleep');
      rejectUnknownFields(obj, ['delayMs']);
      const delayMs = integerField(obj, 'delayMs', { min: 0, max: deps.maxTaskDelayMs, default: 0 });
      await ctx.sleep(delayMs);
      return { slept: delayMs };
    },
  });

  define({
    method: 'calc.add',
    sideEffect: false,
    handle: (params) => {
      const obj = requireObject(params, 'calc.add');
      rejectUnknownFields(obj, ['a', 'b']);
      const a = integerField(obj, 'a', {});
      const b = integerField(obj, 'b', {});
      // Both safe integers but sum may overflow: that must be an error, not
      // a silently-wrapped float.
      const sum = a! + b!;
      if (!Number.isSafeInteger(sum)) {
        throw new MethodError(
          ErrorCode.INVALID_PARAMS,
          'Invalid params: integer overflow in a + b',
          { a, b },
        );
      }
      return sum;
    },
  });

  define({
    method: 'kv.put',
    sideEffect: true,
    idempotencyKeyOf: (params) => {
      // kv.put uses its BUSINESS key as the idempotency key: retries of the
      // same key dedupe naturally. A separate idempotencyKey field is NOT
      // accepted (it is rejected below as an unknown field) so it can never
      // be silently ignored.
      if (params && typeof params === 'object' && !Array.isArray(params)) {
        const k = (params as Record<string, JsonValue>).key;
        return typeof k === 'string' ? k : null;
      }
      return null;
    },
    fingerprint: (params) => stableFingerprint(params),
    handle: (params, ctx: HandlerContext) => {
      const obj = requireObject(params, 'kv.put');
      rejectUnknownFields(obj, ['key', 'value']);
      const key = stringField(obj, 'key', { min: 1, max: 200 });
      if (!('value' in obj)) {
        throw new MethodError(ErrorCode.INVALID_PARAMS, 'Invalid params: "value" is required');
      }
      const revision = deps.domain.list().length + 1;
      try {
        deps.domain.put({
          key,
          value: obj.value,
          revision,
          operationId: ctx.operationId ?? 'unknown',
          createdAt: now(),
        });
      } catch (err) {
        if (err instanceof DuplicateKeyError) {
          // Same business key but a different payload (fingerprint mismatch
          // is normally caught earlier in the kernel); this is the durable
          // domain-level backstop.
          const existing = deps.domain.get(key);
          throw new MethodError(
            ErrorCode.SERVER_ERROR_IDEMPOTENCY_CONFLICT,
            `Key already exists: ${key}`,
            existing ? { operationId: existing.operationId, revision: existing.revision } : undefined,
          );
        }
        throw err;
      }
      return { key, revision, operationId: ctx.operationId };
    },
  });

  define({
    method: 'kv.get',
    sideEffect: false,
    handle: (params) => {
      const obj = requireObject(params, 'kv.get');
      rejectUnknownFields(obj, ['key']);
      const key = stringField(obj, 'key', { min: 1, max: 200 });
      const entry = deps.domain.get(key);
      if (!entry) {
        throw new MethodError(ErrorCode.SERVER_ERROR_NOT_FOUND, `No such key: ${key}`);
      }
      return {
        key: entry.key,
        value: entry.value,
        revision: entry.revision,
        operationId: entry.operationId,
      };
    },
  });

  define({
    method: 'kv.list',
    sideEffect: false,
    handle: (params) => {
      if (params !== undefined) {
        const obj = requireObject(params, 'kv.list');
        rejectUnknownFields(obj, []);
      }
      return { count: deps.domain.list().length, entries: deps.domain.list() };
    },
  });

  define({
    method: 'tasks.start',
    sideEffect: true,
    idempotencyKeyOf: (params) => {
      if (params && typeof params === 'object' && !Array.isArray(params)) {
        const k = (params as Record<string, JsonValue>).idempotencyKey;
        return typeof k === 'string' && k.length > 0 ? k : null;
      }
      return null;
    },
    fingerprint: (params) => stableFingerprint(params),
    handle: (params, ctx) => {
      const obj = requireObject(params, 'tasks.start');
      rejectUnknownFields(obj, ['delayMs', 'label', 'shouldFail', 'idempotencyKey']);
      const delayMs = integerField(obj, 'delayMs', { min: 0, max: deps.maxTaskDelayMs, default: 0 });
      const label = stringField(obj, 'label', { min: 1, max: 100, optional: true });
      const shouldFail = booleanField(obj, 'shouldFail', true) ?? false;
      const operationId = ctx.operationId as string;

      const work = (async (): Promise<JsonValue> => {
        await ctx.sleep(delayMs!);
        if (ctx.signal.aborted) {
          // sleep already rejects on abort; this is a belt-and-braces guard.
          throw new MethodError(
            ErrorCode.SERVER_ERROR_CANCELLED,
            'Task cancelled',
          );
        }
        if (shouldFail) {
          // Generic message: the caller-supplied label is not echoed into the
          // persisted error text. It stays in the task's stored params/result.
          throw new MethodError(TASK_FAILED_CODE, 'Synthetic task failure', { label: label ?? null });
        }
        return { done: true, label: label ?? null, delayedMs: delayMs };
      })();

      return {
        kind: 'detached' as const,
        reply: { operationId, status: 'pending', delayMs, label: label ?? null },
        work,
      };
    },
  });

  // Attached side effect with a delay: unlike tasks.start it does NOT detach,
  // so losing the request connection aborts it and leaves a -32008 record.
  define({
    method: 'kv.putDelayed',
    sideEffect: true,
    fingerprint: (params) => stableFingerprint(params),
    handle: async (params, ctx) => {
      const obj = requireObject(params, 'kv.putDelayed');
      rejectUnknownFields(obj, ['key', 'value', 'delayMs']);
      const key = stringField(obj, 'key', { min: 1, max: 200 });
      if (!('value' in obj)) {
        throw new MethodError(ErrorCode.INVALID_PARAMS, 'Invalid params: "value" is required');
      }
      const delayMs = integerField(obj, 'delayMs', { min: 0, max: deps.maxTaskDelayMs, default: 0 });
      await ctx.sleep(delayMs);
      const revision = deps.domain.list().length + 1;
      try {
        deps.domain.put({
          key,
          value: obj.value,
          revision,
          operationId: ctx.operationId ?? 'unknown',
          createdAt: now(),
        });
      } catch (err) {
        if (err instanceof DuplicateKeyError) {
          throw new MethodError(
            ErrorCode.SERVER_ERROR_IDEMPOTENCY_CONFLICT,
            `Key already exists: ${key}`,
          );
        }
        throw err;
      }
      return { key, revision, operationId: ctx.operationId };
    },
  });

  define({
    method: 'tasks.cancel',
    sideEffect: true,
    // Cancellation is an explicit command about a live operation; it is not
    // idempotency-keyed and is identified purely by targetOperationId.
    handle: (params, ctx) => {
      const obj = requireObject(params, 'tasks.cancel');
      rejectUnknownFields(obj, ['targetOperationId', 'reason']);
      const targetOperationId = stringField(obj, 'targetOperationId', { min: 1, max: 100 });
      const reason = stringField(obj, 'reason', { max: 200, optional: true }) ?? 'Cancelled by client';
      const wasLive = ctx.requestCancel(targetOperationId, reason);
      return { targetOperationId, cancelled: wasLive };
    },
  });

  define({
    method: 'operations.get',
    sideEffect: false,
    handle: (params) => {
      const obj = requireObject(params, 'operations.get');
      rejectUnknownFields(obj, ['operationId']);
      const operationId = stringField(obj, 'operationId', { min: 1, max: 100 });
      const rec = deps.store.getOperation(operationId);
      if (!rec) {
        throw new MethodError(ErrorCode.SERVER_ERROR_NOT_FOUND, `No such operation: ${operationId}`);
      }
      return {
        operationId: rec.operationId,
        method: rec.method,
        status: rec.status,
        idempotencyKey: rec.idempotencyKey,
        live: deps.tasks.isLive(rec.operationId),
        request: rec.request,
        result: rec.result,
        errorCode: rec.errorCode,
        errorMessage: rec.errorMessage,
        startedAt: rec.startedAt,
        finishedAt: rec.finishedAt,
        durationMs: rec.durationMs,
      };
    },
  });

  define({
    method: 'operations.list',
    sideEffect: false,
    handle: (params) => {
      const obj = params === undefined ? {} : requireObject(params, 'operations.list');
      rejectUnknownFields(obj, ['status', 'method', 'idempotencyKey', 'limit']);
      const status = stringField(obj, 'status', { optional: true });
      if (status !== undefined && !['pending', 'succeeded', 'failed', 'cancelled'].includes(status)) {
        throw new MethodError(ErrorCode.INVALID_PARAMS, 'Invalid params: "status" is not a valid operation status');
      }
      const method = stringField(obj, 'method', { max: 100, optional: true });
      const idempotencyKey = stringField(obj, 'idempotencyKey', { max: 200, optional: true });
      const limit = integerField(obj, 'limit', { min: 1, max: 500, optional: true });
      const rows = deps.store.listOperations({
        ...(status !== undefined ? { status: status as 'pending' | 'succeeded' | 'failed' | 'cancelled' } : {}),
        ...(method !== undefined ? { method } : {}),
        ...(idempotencyKey !== undefined ? { idempotencyKey } : {}),
        ...(limit !== undefined ? { limit } : {}),
      });
      return {
        count: rows.length,
        operations: rows.map((r) => ({
          operationId: r.operationId,
          method: r.method,
          status: r.status,
          idempotencyKey: r.idempotencyKey,
          live: deps.tasks.isLive(r.operationId),
          errorCode: r.errorCode,
          errorMessage: r.errorMessage,
          startedAt: r.startedAt,
          finishedAt: r.finishedAt,
          durationMs: r.durationMs,
        })),
      };
    },
  });

  define({
    method: 'diagnostics.events',
    sideEffect: false,
    handle: (params) => {
      const obj = params === undefined ? {} : requireObject(params, 'diagnostics.events');
      rejectUnknownFields(obj, ['batchId', 'connectionId', 'operationId', 'limit']);
      const batchId = stringField(obj, 'batchId', { max: 100, optional: true });
      const connectionId = stringField(obj, 'connectionId', { max: 100, optional: true });
      const operationId = stringField(obj, 'operationId', { max: 100, optional: true });
      const limit = integerField(obj, 'limit', { min: 1, max: 1000, optional: true });
      const events = deps.store.listEvents({
        ...(batchId !== undefined ? { batchId } : {}),
        ...(connectionId !== undefined ? { connectionId } : {}),
        ...(operationId !== undefined ? { operationId } : {}),
        ...(limit !== undefined ? { limit } : {}),
      });
      return {
        count: events.length,
        events: events.map((e) => ({
          eventId: e.eventId,
          ts: e.ts,
          connectionId: e.connectionId,
          batchId: e.batchId,
          rpcId: e.rpcId,
          operationId: e.operationId,
          kind: e.kind,
          decision: e.decision,
          reason: e.reason,
          detail: e.detail,
        })),
      };
    },
  });

  return methods;
}

/**
 * Canonical JSON fingerprint for idempotency conflict detection. Object keys
 * are sorted so `{a:1,b:2}` and `{b:2,a:1}` are the same request — but number
 * 1 and string "1" remain distinct (JSON.stringify preserves that).
 */
export function stableFingerprint(value: JsonValue | undefined): string {
  return canonicalStringify(value ?? null);
}

function canonicalStringify(value: JsonValue): string {
  if (Array.isArray(value)) {
    return `[${value.map((v) => canonicalStringify(v)).join(',')}]`;
  }
  if (value !== null && typeof value === 'object') {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalStringify(value[k]!)}`).join(',')}}`;
  }
  return JSON.stringify(value);
}
