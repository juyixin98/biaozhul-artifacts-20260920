import { describe, expect, it } from 'vitest';
import { redact, containsSensitiveKey } from '../src/diagnostics/redact.js';
import type { RpcResponse } from '../src/contract/protocol.js';
import { memoryHarness } from './helpers/harness.js';

function obj(payload: unknown): RpcResponse {
  if (Array.isArray(payload) || payload === null || typeof payload !== 'object') {
    throw new Error('expected object');
  }
  return payload as RpcResponse;
}

function resultOf(payload: unknown): unknown {
  const resp = obj(payload);
  if ('error' in resp) throw new Error(`unexpected error: ${JSON.stringify(resp.error)}`);
  return resp.result;
}

describe('diagnostics: redaction of sensitive fields', () => {
  it('masks common secret-bearing keys at any depth', () => {
    const input = {
      username: 'alice',
      password: 'hunter2',
      nested: { api_key: 'abcd1234', keep: 1 },
      list: [{ token: 't' }, { authorization: 'Bearer x' }],
      normal: 'visible',
    };
    const out = redact(input) as Record<string, unknown>;
    expect(out.username).toBe('alice');
    expect(out.normal).toBe('visible');
    expect(out.password).toBe('***REDACTED***');
    const nested = out.nested as Record<string, unknown>;
    expect(nested.api_key).toBe('***REDACTED***');
    expect(nested.keep).toBe(1);
    const list = out.list as Array<Record<string, unknown>>;
    expect(list[0]?.token).toBe('***REDACTED***');
    expect(list[1]?.authorization).toBe('***REDACTED***');
  });

  it('detects sensitive keys for flagging', () => {
    expect(containsSensitiveKey({ secret: 1 })).toBe(true);
    expect(containsSensitiveKey({ credential: 1 })).toBe(true);
    expect(containsSensitiveKey({ passwd: 1 })).toBe(true);
    expect(containsSensitiveKey({ username: 1 })).toBe(false);
    expect(containsSensitiveKey({ a: { b: { token: 'x' } } })).toBe(true);
  });

  it('catches camelCase / header-style / compound sensitive names without over-matching', () => {
    const out = redact({
      passwordHash: 'p',
      accessToken: 't',
      refreshToken: 'r',
      clientSecret: 'cs',
      authToken: 'at',
      idToken: 'it',
      apiKey: 'k',
      'X-Auth-Token': 'h',
      client_secret: 's',
      authentication: 'a',
      privateKey: 'pk',
      myJwt: 'j',
      // a sensitive key masks its WHOLE value
      tokens: ['t1', 't2'],
      // benign lookalikes must remain visible
      username: 'bob',
      author: 'carol',
      category: 'c',
    }) as Record<string, unknown>;
    expect(out.passwordHash).toBe('***REDACTED***');
    expect(out.accessToken).toBe('***REDACTED***');
    expect(out.refreshToken).toBe('***REDACTED***');
    expect(out.clientSecret).toBe('***REDACTED***');
    expect(out.authToken).toBe('***REDACTED***');
    expect(out.idToken).toBe('***REDACTED***');
    expect(out.apiKey).toBe('***REDACTED***');
    expect(out['X-Auth-Token']).toBe('***REDACTED***');
    expect(out.client_secret).toBe('***REDACTED***');
    expect(out.authentication).toBe('***REDACTED***');
    expect(out.privateKey).toBe('***REDACTED***');
    expect(out.myJwt).toBe('***REDACTED***');
    expect(out.tokens).toBe('***REDACTED***');
    expect(out.username).toBe('bob');
    expect(out.author).toBe('carol');
    expect(out.category).toBe('c');
  });

  it('never persists the raw secret in an operation record', async () => {
    const h = memoryHarness();
    await h.invoke(
      JSON.stringify({
        jsonrpc: '2.0',
        method: 'kv.put',
        params: { key: 'cred-1', value: { password: 'super-secret', ok: 1 } },
        id: 1,
      }),
    );
    const resp = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.list', params: { method: 'kv.put' }, id: 2 }),
        )
      ).payload,
    );
    // operations.list doesn't return the request body; fetch the full record.
    if ('error' in resp) throw new Error('expected list');
    const firstId = (resp.result as {
      operations: Array<{ operationId: string }>;
    }).operations[0]?.operationId;

    const detail = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'operations.get', params: { operationId: firstId }, id: 3 }),
        )
      ).payload,
    );
    if ('error' in detail) throw new Error('expected operation detail');
    const request = (detail.result as { request: { value: Record<string, unknown> } }).request;
    expect(request.value.password).toBe('***REDACTED***');
    expect(request.value.ok).toBe(1);
    expect(JSON.stringify(request)).not.toContain('super-secret');
  });
});

describe('diagnostics: events explain accept / reject / indeterminate with ids', () => {
  it('records accepted, rejected and indeterminate decisions with correlation ids', async () => {
    const h = memoryHarness({ logLevel: 'info' });

    // accepted
    await h.invoke(JSON.stringify({ jsonrpc: '2.0', method: 'ping', id: 1 }));
    // rejected: unknown method
    await h.invoke(JSON.stringify({ jsonrpc: '2.0', method: 'ghost', id: 2 }));
    // indeterminate: parse error
    await h.invoke('{{{');
    // rejected: duplicate id
    await h.invoke(
      JSON.stringify([
        { jsonrpc: '2.0', method: 'ping', id: 5 },
        { jsonrpc: '2.0', method: 'ping', id: 5 },
      ]),
    );

    const resp = obj(
      (
        await h.invoke(
          JSON.stringify({ jsonrpc: '2.0', method: 'diagnostics.events', params: { limit: 100 }, id: 9 }),
        )
      ).payload,
    );
    if ('error' in resp) throw new Error('expected events result');
    const events = (resp.result as {
      events: Array<{ decision: string; reason: string; rpcId: string | null; kind: string }>;
    }).events;

    const reasons = events.map((e) => e.reason);
    expect(reasons).toContain('envelope-valid');
    expect(reasons).toContain('no-such-method');
    expect(reasons).toContain('body-is-not-json');
    expect(reasons).toContain('duplicate-rpc-id-within-batch');

    const decisions = new Set(events.map((e) => e.decision));
    expect(decisions.has('accepted')).toBe(true);
    expect(decisions.has('rejected')).toBe(true);
    expect(decisions.has('indeterminate')).toBe(true);

    // The parse-error event is associated with a null rpc id, not a fabricated one.
    const parseEvent = events.find((e) => e.reason === 'body-is-not-json');
    expect(parseEvent?.rpcId).toBeNull();
  });

  it('never persists a secret-bearing raw excerpt on a parse error', async () => {
    const h = memoryHarness({ logLevel: 'debug' });
    // Unterminated JSON containing a secret value.
    await h.invoke('{"jsonrpc":"2.0","method":"ping","params":{"token":"SUPERSECRET","x":');

    const events = h.store.listEvents({ limit: 10 });
    const parseEvent = events.find((e) => e.reason === 'body-is-not-json');
    expect(parseEvent).toBeDefined();
    const serialized = JSON.stringify(parseEvent);
    expect(serialized).not.toContain('SUPERSECRET');
    expect(parseEvent?.detail).toEqual({ rawByteLength: expect.any(Number), oversized: false });
    // And nothing on the human sink either.
    expect(h.logLines.join('\n')).not.toContain('SUPERSECRET');
  });

  it('emits human-readable sink lines tagged by decision and correlation', async () => {
    const h = memoryHarness({ logLevel: 'debug' });
    await h.invoke(
      JSON.stringify({ jsonrpc: '2.0', method: 'kv.put', params: { key: 'sink', value: 1 }, id: 42 }),
    );
    const line = h.logLines.find((l) => l.includes('operation-started'));
    expect(line).toBeDefined();
    // accepted tag and the rendered rpc id appear
    expect(/ACCEPT/.test(line!)).toBe(true);
    expect(line!).toContain('rpc=42');
  });
});
