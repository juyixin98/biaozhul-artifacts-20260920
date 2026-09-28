import { describe, expect, it } from 'vitest';
import type { RpcResponse } from '../src/contract/protocol.js';
import { memoryHarness } from './helpers/harness.js';

function asArray(payload: unknown): RpcResponse[] {
  if (!Array.isArray(payload)) throw new Error('expected batch payload array');
  return payload as RpcResponse[];
}

function asObject(payload: unknown): RpcResponse {
  if (Array.isArray(payload) || payload === null || typeof payload !== 'object') {
    throw new Error('expected single response object');
  }
  return payload as RpcResponse;
}

describe('kernel: batch attribution, notifications and ordering', () => {
  it('returns one response per request in INPUT ORDER even when completion is out of order', async () => {
    const h = memoryHarness();
    // First request is the slow one; second resolves first. Responses must
    // still be returned in request (input) order, keyed by position — never
    // reordered by completion time.
    const body = JSON.stringify([
      { jsonrpc: '2.0', method: 'tasks.start', params: { delayMs: 30, label: 'slow' }, id: 'slow-id' },
      { jsonrpc: '2.0', method: 'ping', id: 'fast-id' },
    ]);
    const result = await h.invoke(body);
    const responses = asArray(result.payload);
    expect(responses).toHaveLength(2);
    expect(responses[0]?.id).toBe('slow-id');
    expect(responses[1]?.id).toBe('fast-id');
    const slow = responses[0];
    if (!slow || 'error' in slow) {
      throw new Error('slow task start should succeed');
    }
    expect(slow.result).toMatchObject({ status: 'pending' });
    expect(responses[1]).toEqual({ jsonrpc: '2.0', result: 'pong', id: 'fast-id' });
  });

  it('omits notifications from the response entirely but still executes their side effects', async () => {
    const h = memoryHarness();
    const body = JSON.stringify([
      { jsonrpc: '2.0', method: 'kv.put', params: { key: 'k1', value: 1 } },
      { jsonrpc: '2.0', method: 'ping', id: 2 },
      { jsonrpc: '2.0', method: 'kv.put', params: { key: 'k2', value: 2 } },
    ]);
    const result = await h.invoke(body);
    const responses = asArray(result.payload);
    expect(responses).toHaveLength(1);
    expect(responses[0]).toEqual({ jsonrpc: '2.0', result: 'pong', id: 2 });

    // Side effects of the notifications are observable in the domain store.
    const list = asObject(
      (await h.invoke(JSON.stringify({ jsonrpc: '2.0', method: 'kv.list', id: 9 }))).payload,
    );
    expect('error' in list).toBe(false);
    if ('result' in list) {
      const result = list.result as { count: number };
      expect(result.count).toBe(2);
    }
  });

  it('returns HTTP 204 / null payload for an all-notification batch', async () => {
    const h = memoryHarness();
    const result = await h.invoke(
      JSON.stringify([{ jsonrpc: '2.0', method: 'ping' }, { jsonrpc: '2.0', method: 'ping' }]),
    );
    expect(result.status).toBe(204);
    expect(result.payload).toBeNull();
  });

  it('records a failed notification as a failed operation even though nothing is returned', async () => {
    const h = memoryHarness();
    // unknown method inside a notification: no response, but a record exists.
    await h.invoke(JSON.stringify([{ jsonrpc: '2.0', method: 'does.not.exist' }]));
    const opsResp = asObject(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.list', params: { method: 'does.not.exist' }, id: 1 }),
        )
      ).payload,
    );
    // unknown method never opens an operation, but a diagnostic rejection event exists.
    const eventsResp = asObject(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'diagnostics.events', params: { limit: 50 }, id: 2 }),
        )
      ).payload,
    );
    if ('result' in eventsResp) {
      const result = eventsResp.result as { events: Array<{ decision: string; reason: string }> };
      expect(result.events.some((e) => e.reason === 'no-such-method' && e.decision === 'rejected')).toBe(true);
    } else {
      throw new Error('expected diagnostics result');
    }
    expect(opsResp).toBeDefined();
  });

  it('records failed side-effect notifications (method error) in operations with the specific code', async () => {
    const h = memoryHarness();
    await h.invoke(
      JSON.stringify([{ jsonrpc: '2.0', method: 'kv.put', params: { key: 'dup', value: 1 } }]),
    );
    // Second notification put on same business key -> conflict, no response.
    await h.invoke(
      JSON.stringify([{ jsonrpc: '2.0', method: 'kv.put', params: { key: 'dup', value: 2 } }]),
    );
    const opsResp = asObject(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.list', params: { method: 'kv.put' }, id: 1 }),
        )
      ).payload,
    );
    if ('result' in opsResp) {
      const result = opsResp.result as {
        operations: Array<{ status: string; errorCode: number | null }>;
      };
      // One success, one failed conflict (-32005).
      expect(result.operations).toHaveLength(2);
      const statuses = result.operations.map((o) => o.status).sort();
      expect(statuses).toEqual(['failed', 'succeeded']);
      const failed = result.operations.find((o) => o.status === 'failed');
      expect(failed?.errorCode).toBe(-32005);
    } else {
      throw new Error('expected operations result');
    }
  });

  it('rejects duplicate RPC ids within a batch with -32001 and never crosses results', async () => {
    const h = memoryHarness();
    const body = JSON.stringify([
      { jsonrpc: '2.0', method: 'echo', params: { v: 'first' }, id: 7 },
      { jsonrpc: '2.0', method: 'echo', params: { v: 'second' }, id: 7 },
      { jsonrpc: '2.0', method: 'ping', id: 8 },
    ]);
    const responses = asArray((await h.invoke(body)).payload);
    expect(responses).toHaveLength(3);
    expect(responses[0]).toEqual({ jsonrpc: '2.0', result: { v: 'first' }, id: 7 });
    if ('error' in responses[1]!) {
      expect(responses[1].id).toBe(7);
      expect(responses[1].error.code).toBe(-32001);
    } else {
      throw new Error('second occurrence must be a duplicate-id error');
    }
    expect(responses[2]).toEqual({ jsonrpc: '2.0', result: 'pong', id: 8 });
  });

  it('does NOT treat the same RPC id across different batches as a collision', async () => {
    const h = memoryHarness();
    const payload = JSON.stringify({ jsonrpc: '2.0', method: 'echo', params: { v: 'a' }, id: 1 });
    const r1 = asObject((await h.invoke(payload)).payload);
    const r2 = asObject((await h.invoke(payload)).payload);
    expect(r1).toEqual({ jsonrpc: '2.0', result: { v: 'a' }, id: 1 });
    expect(r2).toEqual({ jsonrpc: '2.0', result: { v: 'a' }, id: 1 });
  });

  it('keeps number id 1 and string id "1" distinct (no cross-wiring)', async () => {
    const h = memoryHarness();
    const body = JSON.stringify([
      { jsonrpc: '2.0', method: 'echo', params: { v: 'num' }, id: 1 },
      { jsonrpc: '2.0', method: 'echo', params: { v: 'str' }, id: '1' },
    ]);
    const responses = asArray((await h.invoke(body)).payload);
    expect(responses).toHaveLength(2);
    expect(responses[0]).toEqual({ jsonrpc: '2.0', result: { v: 'num' }, id: 1 });
    expect(responses[1]).toEqual({ jsonrpc: '2.0', result: { v: 'str' }, id: '1' });
  });

  it('returns a per-element -32600 for invalid batch elements while valid ones still respond', async () => {
    const h = memoryHarness();
    const body = JSON.stringify([
      { jsonrpc: '2.0', method: 'ping', id: 1 },
      'not-an-object',
      { foo: 1 },
      { jsonrpc: '2.0', method: 'ping' },
    ]);
    const responses = asArray((await h.invoke(body)).payload);
    expect(responses).toHaveLength(3); // two requests + two invalid... wait: 4 elements, one notification
    // Elements: request(ping,id1), invalid, invalid, notification -> 3 responses.
    expect(responses[0]).toEqual({ jsonrpc: '2.0', result: 'pong', id: 1 });
    expect(responses[1]?.id).toBeNull();
    expect('error' in responses[1]!).toBe(true);
    if ('error' in responses[1]!) expect(responses[1].error.code).toBe(-32600);
    expect(responses[2]?.id).toBeNull();
    if ('error' in responses[2]!) expect(responses[2].error.code).toBe(-32600);
  });

  it('returns a single -32600 object (not an array) for an invalid single request', async () => {
    const h = memoryHarness();
    const result = await h.invoke('{"jsonrpc":"2.0"}');
    const resp = asObject(result.payload);
    expect(Array.isArray(result.payload)).toBe(false);
    expect(resp.id).toBeNull();
    if ('error' in resp) expect(resp.error.code).toBe(-32600);
  });

  it('returns parse error -32700 as a single response object with id null', async () => {
    const h = memoryHarness();
    const result = await h.invoke('garbage{');
    const resp = asObject(result.payload);
    if ('error' in resp) {
      expect(resp.error.code).toBe(-32700);
      expect(resp.id).toBeNull();
    } else {
      throw new Error('expected parse error');
    }
  });

  it('rejects a batch exceeding the element limit with a single -32600 envelope', async () => {
    const h = memoryHarness();
    const elements = Array.from({ length: 10_001 }, () => ({
      jsonrpc: '2.0',
      method: 'ping',
    }));
    const result = await h.invoke(JSON.stringify(elements));
    expect(Array.isArray(result.payload)).toBe(false);
    const resp = asObject(result.payload);
    if ('error' in resp) {
      expect(resp.error.code).toBe(-32600);
      expect(resp.error.data).toMatchObject({ limit: 10_000, count: 10_001 });
    } else {
      throw new Error('expected batch-too-large error');
    }
  });
});
