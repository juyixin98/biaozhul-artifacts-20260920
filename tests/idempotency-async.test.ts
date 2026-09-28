import { describe, expect, it } from 'vitest';
import type { RpcResponse } from '../src/contract/protocol.js';
import { abortable, flush, memoryHarness } from './helpers/harness.js';

function obj(payload: unknown): RpcResponse {
  if (Array.isArray(payload) || payload === null || typeof payload !== 'object') {
    throw new Error('expected single response object');
  }
  return payload as RpcResponse;
}

function result(resp: RpcResponse): unknown {
  if ('error' in resp) throw new Error(`unexpected error: ${JSON.stringify(resp.error)}`);
  return resp.result;
}

function errorCode(resp: RpcResponse): number {
  if (!('error' in resp)) throw new Error('expected error response');
  return resp.error.code;
}

describe('idempotency: independent operation ids, RPC id is never the key', () => {
  it('replays the stored result for a repeated kv.put and performs the side effect only once', async () => {
    const h = memoryHarness();
    const first = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'kv.put', params: { key: 'k', value: 'v1' }, id: 1 }),
        )
      ).payload,
    );
    const firstResult = result(first) as { revision: number; operationId: string };
    expect(firstResult.revision).toBe(1);

    // Same key AND same payload: dedupe -> identical stored result, no 2nd effect.
    const second = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'kv.put', params: { key: 'k', value: 'v1' }, id: 2 }),
        )
      ).payload,
    );
    expect(result(second)).toEqual(firstResult);

    const list = obj(
      (await h.invoke(JSON.stringify({ jsonrpc: '2.0', method: 'kv.list', id: 3 }))).payload,
    );
    expect((result(list) as { count: number }).count).toBe(1);

    // A successful replay must NOT mint a second operation.
    const ops = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.list', params: { method: 'kv.put' }, id: 4 }),
        )
      ).payload,
    );
    expect((result(ops) as { operations: unknown[] }).operations).toHaveLength(1);
  });

  it('rejects an explicit idempotencyKey field on kv.put rather than silently ignoring it', async () => {
    const h = memoryHarness();
    const resp = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'kv.put', params: { key: 'k', value: 1, idempotencyKey: 'x' }, id: 1 }),
        )
      ).payload,
    );
    expect(errorCode(resp)).toBe(-32602);
  });

  it('rejects same key with DIFFERENT params with -32005 and records an audit operation', async () => {
    const h = memoryHarness();
    const first = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'kv.put', params: { key: 'k', value: 'v1' }, id: 1 }),
        )
      ).payload,
    );
    const originalOpId = (result(first) as { operationId: string }).operationId;

    const conflict = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'kv.put', params: { key: 'k', value: 'DIFFERENT' }, id: 2 }),
        )
      ).payload,
    );
    expect(errorCode(conflict)).toBe(-32005);
    if ('error' in conflict) {
      expect(conflict.error.data).toMatchObject({
        existingOperationId: originalOpId,
        reason: 'fingerprint-mismatch',
      });
      // The rejected attempt has its OWN operation id, distinct from original.
      const attemptId = (conflict.error.data as { attemptOperationId: string }).attemptOperationId;
      expect(attemptId).not.toBe(originalOpId);
    }

    // Domain state untouched by the rejected attempt.
    const list = obj(
      (await h.invoke(JSON.stringify({ jsonrpc: '2.0', method: 'kv.list', id: 3 }))).payload,
    );
    const entries = (result(list) as { entries: Array<{ value: unknown }> }).entries;
    expect(entries).toHaveLength(1);
    expect(entries[0]?.value).toBe('v1');

    // Audit: one succeeded original + one failed attempt pointing at original.
    const ops = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.list', params: { method: 'kv.put' }, id: 4 }),
        )
      ).payload,
    );
    const rows = result(ops) as {
      operations: Array<{ status: string; errorCode: number | null; replayedFromOperationId?: string }>;
    };
    // operations.list projection doesn't expose replayedFromOperationId; fetch the attempt row.
    expect(rows.operations).toHaveLength(2);
    const failed = rows.operations.find((o) => o.status === 'failed');
    expect(failed?.errorCode).toBe(-32005);
    const succeeded = rows.operations.find((o) => o.status === 'succeeded');
    expect(succeeded?.errorCode).toBeNull();
  });

  it('rejects a second start with the same explicit key while the first is pending (-32005 in-progress)', async () => {
    const h = memoryHarness();
    const params = { delayMs: 40, label: 'job', idempotencyKey: 'job-1' };
    const first = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.start', params, id: 1 }),
        )
      ).payload,
    );
    const firstOp = (result(first) as { operationId: string }).operationId;

    const second = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.start', params, id: 2 }),
        )
      ).payload,
    );
    expect(errorCode(second)).toBe(-32005);
    if ('error' in second) {
      expect(second.error.data).toMatchObject({ reason: 'in-progress', existingOperationId: firstOp });
    }

    await flush(60);
    // After completion, same key replays the stored terminal result, still no new side effect.
    const third = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.start', params, id: 3 }),
        )
      ).payload,
    );
    expect(result(third)).toEqual({ done: true, label: 'job', delayedMs: 40 });
  });

  it('mints distinct operation ids even when RPC ids repeat across batches', async () => {
    const h = memoryHarness();
    const r1 = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.start', params: { delayMs: 0 }, id: 99 }),
        )
      ).payload,
    );
    const r2 = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.start', params: { delayMs: 0 }, id: 99 }),
        )
      ).payload,
    );
    const op1 = (result(r1) as { operationId: string }).operationId;
    const op2 = (result(r2) as { operationId: string }).operationId;
    expect(op1).not.toBe(op2);
    expect(op1.startsWith('op_')).toBe(true);
  });
});

describe('async tasks: lifecycle, cancellation and connection loss', () => {
  it('moves a detached task from pending to succeeded with its recorded result', async () => {
    const h = memoryHarness();
    const started = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.start', params: { delayMs: 20, label: 't' }, id: 1 }),
        )
      ).payload,
    );
    const opId = (result(started) as { operationId: string; status: string }).operationId;
    expect(result(started)).toMatchObject({ status: 'pending' });

    let pending = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.get', params: { operationId: opId }, id: 2 }),
        )
      ).payload,
    );
    expect(result(pending)).toMatchObject({ status: 'pending', live: true });

    await flush(40);
    const done = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.get', params: { operationId: opId }, id: 3 }),
        )
      ).payload,
    );
    expect(result(done)).toMatchObject({
      status: 'succeeded',
      live: false,
      result: { done: true, label: 't', delayedMs: 20 },
    });
  });

  it('records a synthetic failing task as failed with the business code -32050', async () => {
    const h = memoryHarness();
    const started = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.start', params: { delayMs: 5, shouldFail: true, label: 'boom' }, id: 1 }),
        )
      ).payload,
    );
    const opId = (result(started) as { operationId: string }).operationId;
    await flush(20);
    const rec = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.get', params: { operationId: opId }, id: 2 }),
        )
      ).payload,
    );
    expect(result(rec)).toMatchObject({ status: 'failed', errorCode: -32050 });
  });

  it('cancels a live task, recording status cancelled with code -32007', async () => {
    const h = memoryHarness();
    const started = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.start', params: { delayMs: 300, label: 'long' }, id: 1 }),
        )
      ).payload,
    );
    const opId = (result(started) as { operationId: string }).operationId;

    const cancel = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.cancel', params: { targetOperationId: opId, reason: 'test cancel' }, id: 2 }),
        )
      ).payload,
    );
    expect(result(cancel)).toEqual({ targetOperationId: opId, cancelled: true });
    await flush(10);

    const rec = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.get', params: { operationId: opId }, id: 3 }),
        )
      ).payload,
    );
    expect(result(rec)).toMatchObject({ status: 'cancelled', errorCode: -32007, live: false });

    // Cancelling an already-finished task reports not-live, without error.
    const again = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.cancel', params: { targetOperationId: opId }, id: 4 }),
        )
      ).payload,
    );
    expect(result(again)).toEqual({ targetOperationId: opId, cancelled: false });
  });

  it('keeps running a detached task after the requesting connection is lost', async () => {
    const h = memoryHarness();
    const conn = abortable();
    const started = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.start', params: { delayMs: 30, label: 'survivor' }, id: 1 }),
          { signal: conn.signal },
        )
      ).payload,
    );
    const opId = (result(started) as { operationId: string }).operationId;

    // Client goes away AFTER receiving the immediate "pending" reply.
    conn.abort('client closed');
    await flush(50);

    // A fresh, healthy connection queries the outcome: task still completed.
    const rec = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.get', params: { operationId: opId }, id: 2 }),
        )
      ).payload,
    );
    expect(result(rec)).toMatchObject({
      status: 'succeeded',
      result: { done: true, label: 'survivor', delayedMs: 30 },
    });
  });

  it('starts a detached task from a NOTIFICATION (no response) and it still completes and is queryable', async () => {
    const h = memoryHarness();
    const out = await h.invoke(
      JSON.stringify({ jsonrpc: '2.0', method: 'tasks.start', params: { delayMs: 10, label: 'ntf' } }),
    );
    expect(out.status).toBe(204);
    expect(out.payload).toBeNull();
    await flush(30);

    const ops = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.list', params: { method: 'tasks.start' }, id: 1 }),
        )
      ).payload,
    );
    const rows = result(ops) as { operations: Array<{ status: string }> };
    expect(rows.operations).toHaveLength(1);
    expect(rows.operations[0]?.status).toBe('succeeded');
  });
});

describe('boundary arithmetic and strict params', () => {
  it('returns -32602 on integer overflow instead of wrapping to a float', async () => {
    const h = memoryHarness();
    const resp = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'calc.add', params: { a: Number.MAX_SAFE_INTEGER, b: 1 }, id: 1 }),
        )
      ).payload,
    );
    expect(errorCode(resp)).toBe(-32602);
  });

  it('rejects a boolean where an integer is required (no truthy coercion)', async () => {
    const h = memoryHarness();
    const resp = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'calc.add', params: { a: true, b: 2 }, id: 1 }),
        )
      ).payload,
    );
    expect(errorCode(resp)).toBe(-32602);
  });

  it('rejects a delay over the configured maximum', async () => {
    const h = memoryHarness({ maxTaskDelayMs: 100 });
    const resp = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'tasks.start', params: { delayMs: 101 }, id: 1 }),
        )
      ).payload,
    );
    expect(errorCode(resp)).toBe(-32602);
  });

  it('returns -32601 for an unknown method, distinct from -32602/-32603', async () => {
    const h = memoryHarness();
    const resp = obj(
      (await h.invoke(JSON.stringify({ jsonrpc: '2.0', method: 'nope', id: 1 }))).payload,
    );
    expect(errorCode(resp)).toBe(-32601);
  });
});
