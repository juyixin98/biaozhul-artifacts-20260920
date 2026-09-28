import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app.js';
import type { AppConfig } from '../src/config.js';
import { tempDbPath } from './helpers/harness.js';

/**
 * Real HTTP integration tests. These go over TCP (not direct kernel calls), so
 * they exercise Fastify body handling, status codes (204), and genuine client
 * connection interruption via AbortController.
 */

let app: ReturnType<typeof buildApp>;
let server: FastifyInstance;
let baseUrl: string;

beforeAll(async () => {
  const config: AppConfig = {
    host: '127.0.0.1',
    port: 0,
    dbPath: tempDbPath(),
    maxTaskDelayMs: 2000,
    logLevel: 'silent',
  };
  app = buildApp(config);
  await app.server.listen({ host: '127.0.0.1', port: 0 });
  const address = app.server.server.address();
  if (address === null || typeof address === 'string') throw new Error('no listen address');
  baseUrl = `http://127.0.0.1:${address.port}`;
  server = app.server;
});

afterAll(async () => {
  await app.tasks.drain(3000);
  await server.close();
  app.state.close();
  app.domain.close();
});

async function post(body: string, init?: RequestInit): Promise<Response> {
  return fetch(baseUrl + '/', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body,
    ...init,
  });
}

async function rpc(body: unknown, id: number): Promise<unknown> {
  const resp = await post(JSON.stringify(body));
  const json = (await resp.json()) as { result?: unknown };
  return json.result;
}

describe('HTTP integration: mixed batch with out-of-order completion', () => {
  it('returns 200 and responses aligned to request order, excluding notifications', async () => {
    const batch = [
      { jsonrpc: '2.0', method: 'tasks.start', params: { delayMs: 40, label: 'slow-http' }, id: 'a' },
      { jsonrpc: '2.0', method: 'kv.put', params: { key: 'http-n1', value: 1 } }, // notification
      { jsonrpc: '2.0', method: 'ping', id: 'b' },
      42, // invalid batch element
      { jsonrpc: '2.0', method: 'echo', params: { v: 'mixed' }, id: null },
    ];
    const resp = await post(JSON.stringify(batch));
    expect(resp.status).toBe(200);
    const json = (await resp.json()) as Array<Record<string, unknown>>;
    expect(json).toHaveLength(4); // 3 requests (a,b,null) + 1 invalid; notification excluded

    expect(json[0]?.id).toBe('a');
    expect((json[0]?.result as Record<string, unknown>)?.status).toBe('pending');

    expect(json[1]).toEqual({ jsonrpc: '2.0', result: 'pong', id: 'b' });

    // Invalid element echo id is null, code -32600, tagged as batch element.
    const invalid = json[2] as { error?: { code: number; data?: { reason: string } }; id: null };
    expect(invalid.id).toBeNull();
    expect(invalid.error?.code).toBe(-32600);
    expect(invalid.error?.data?.reason).toBe('invalid-batch-element');

    // id null is preserved exactly (not conflated with the invalid element).
    expect(json[3]).toEqual({ jsonrpc: '2.0', result: { v: 'mixed' }, id: null });
  });

  it('returns 204 with empty body for an all-notification batch', async () => {
    const resp = await post(
      JSON.stringify([
        { jsonrpc: '2.0', method: 'ping' },
        { jsonrpc: '2.0', method: 'kv.put', params: { key: 'http-n2', value: 9 } },
      ]),
    );
    expect(resp.status).toBe(204);
    expect(await resp.text()).toBe('');
  });

  it('returns -32700 as JSON-RPC body with HTTP 200 for malformed JSON', async () => {
    const resp = await post('not json at all');
    expect(resp.status).toBe(200);
    const json = (await resp.json()) as { error: { code: number }; id: null };
    expect(json.error.code).toBe(-32700);
    expect(json.id).toBeNull();
  });

  it('treats an empty body as a parse error (-32700), not an HTTP 400', async () => {
    const resp = await fetch(baseUrl + '/', { method: 'POST', body: '' });
    expect(resp.status).toBe(200);
    const json = (await resp.json()) as { error: { code: number; message: string }; id: null };
    expect(json.error.code).toBe(-32700);
    expect(json.error.message).toMatch(/empty/i);
  });

  it('notification side effect actually lands despite having no response', async () => {
    await post(JSON.stringify([{ jsonrpc: '2.0', method: 'kv.put', params: { key: 'http-effect', value: 77 } }]));
    const got = (await rpc(
      { jsonrpc: '2.0', method: 'kv.get', params: { key: 'http-effect' }, id: 1 },
      1,
    )) as { value: number; revision: number };
    expect(got.value).toBe(77);
    expect(typeof got.revision).toBe('number');
  });
});

describe('HTTP integration: genuine client disconnection', () => {
  it('a detached task survives the client aborting and finishes with a queryable result', async () => {
    const controller = new AbortController();
    const resp = await post(
      JSON.stringify({
        jsonrpc: '2.0',
        method: 'tasks.start',
        params: { delayMs: 40, label: 'http-survivor' },
        id: 1,
      }),
      { signal: controller.signal },
    );
    const started = (await resp.json()) as { result: { operationId: string } };
    const opId = started.result.operationId;

    // Abort a DIFFERENT concern: simulate the original client going away.
    controller.abort();
    await new Promise((r) => setTimeout(r, 80));

    // Healthy follow-up request sees the detached task completed successfully.
    const rec = (await rpc(
      { jsonrpc: '2.0', method: 'operations.get', params: { operationId: opId }, id: 2 },
      2,
    )) as { status: string; result: { label: string } };
    expect(rec.status).toBe('succeeded');
    expect(rec.result.label).toBe('http-survivor');
  });

  it('records an aborted ATTACHED side effect as failed (-32008) after client drops mid-request', async () => {
    // Start the request, then abort the socket before the attached delayed put
    // finishes. We discover the operation afterwards via a listing.
    const controller = new AbortController();
    const fetchPromise = post(
      JSON.stringify({
        jsonrpc: '2.0',
        method: 'kv.putDelayed',
        params: { key: 'aborted-attached', value: 1, delayMs: 500 },
        id: 1,
      }),
      { signal: controller.signal },
    );
    // Give the server a moment to begin processing, then cut the connection.
    await new Promise((r) => setTimeout(r, 30));
    controller.abort();
    // Whether the client fetch rejects or races a late response is transport
    // timing; the durable, asserted contract is the server-side record below.
    await fetchPromise.catch(() => undefined);

    // Allow the abort to propagate and the operation to be finalized.
    await new Promise((r) => setTimeout(r, 50));

    const ops = (await rpc(
      { jsonrpc: '2.0', method: 'operations.list', params: { method: 'kv.putDelayed' }, id: 3 },
      3,
    )) as {
      operations: Array<{ status: string; errorCode: number; operationId: string }>;
    };
    expect(ops.operations.length).toBeGreaterThanOrEqual(1);
    const rec = ops.operations.find((o) => o.status === 'failed');
    expect(rec).toBeDefined();
    expect(rec?.errorCode).toBe(-32008);

    // And the side effect did NOT land.
    const list = (await rpc({ jsonrpc: '2.0', method: 'kv.list', id: 4 }, 4)) as {
      entries: Array<{ key: string }>;
    };
    expect(list.entries.some((e) => e.key === 'aborted-attached')).toBe(false);
  });
});
