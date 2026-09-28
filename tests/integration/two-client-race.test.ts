/**
 * End-to-end two-client concurrency against a real listening server.
 *
 * Both clients start from the same v1 ETag and fire their conditional PUT in
 * parallel. Because condition check + write are one synchronous IMMEDIATE
 * transaction, exactly one write can win: the other must receive 412 against
 * the freshly committed version. We then replay the documented recovery path
 * (refresh ETag, retry) and assert the final history is a gapless [1,2,3] —
 * proving there is no lost update and no duplicate version.
 */
import { describe, expect, it } from 'vitest';
import { AddressInfo } from 'node:net';
import { buildTempApp, TempApp } from './replay.js';

async function listen(env: TempApp): Promise<string> {
  await env.app.listen({ port: 0, host: '127.0.0.1' });
  const address = env.app.server.address() as AddressInfo;
  return `http://127.0.0.1:${address.port}`;
}

interface ResourceBody {
  readonly version: number;
  readonly body: { text?: string };
}

interface ErrorBody {
  readonly error: {
    readonly code: string;
    readonly trace?: Array<Record<string, unknown>>;
  };
}

async function putJson(
  base: string,
  path: string,
  body: unknown,
  headers: Record<string, string>
): Promise<{ status: number; etag: string; json: ResourceBody | ErrorBody | null; versionHeader: string }> {
  const res = await fetch(base + path, {
    method: 'PUT',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(body)
  });
  const text = await res.text();
  return {
    status: res.status,
    etag: res.headers.get('etag') ?? '',
    versionHeader: res.headers.get('x-resource-version') ?? '',
    json: text ? (JSON.parse(text) as ResourceBody | ErrorBody) : null
  };
}

async function getJson(
  base: string,
  path: string,
  headers: Record<string, string> = {}
): Promise<{ status: number; etag: string; json: ResourceBody }> {
  const res = await fetch(base + path, { method: 'GET', headers });
  const text = await res.text();
  return { status: res.status, etag: res.headers.get('etag') ?? '', json: JSON.parse(text) as ResourceBody };
}

describe('two-client concurrent conditional updates', () => {
  it('parallel If-Match writes from the same base version: one wins, one gets 412, then recovers', async () => {
    const env = buildTempApp(true);
    const base = await listen(env);
    try {
      // Shared starting state.
      const created = await putJson(base, '/resources/doc', { text: 'v0' }, { 'X-Request-Id': 'seed' });
      expect(created.status).toBe(201);
      const baseEtag = created.etag;
      expect(baseEtag).toMatch(/"v1-/);

      // Fire both clients concurrently against the SAME stale-after-commit base.
      const [clientA, clientB] = await Promise.all([
        putJson(base, '/resources/doc', { text: 'A-write' }, {
          'If-Match': baseEtag, 'X-Request-Id': 'client-A'
        }),
        putJson(base, '/resources/doc', { text: 'B-write' }, {
          'If-Match': baseEtag, 'X-Request-Id': 'client-B'
        })
      ]);

      const statuses = [clientA.status, clientB.status].sort();
      expect(statuses).toEqual([200, 412]);

      const winner = clientA.status === 200 ? clientA : clientB;
      const loser = clientA.status === 412 ? clientA : clientB;
      const winnerId = winner === clientA ? 'client-A' : 'client-B';
      const loserId = loser === clientA ? 'client-A' : 'client-B';

      const loserError = loser.json as ErrorBody;
      // Winner advanced exactly one version; the 412 reports the live ETag.
      expect(winner.versionHeader).toBe('2');
      expect(winner.etag).toMatch(/"v2-/);
      expect(loserError.error.code).toBe('PRECONDITION_IF_MATCH_FAILED');
      expect(loser.etag).toBe(winner.etag);
      expect(loserError.error.trace?.[0]).toMatchObject({
        header: 'If-Match', comparison: 'strong', result: 'no-match'
      });

      // The loser did not mutate anything: current version is still 2.
      const mid = await getJson(base, '/resources/doc');
      expect(mid.json.version).toBe(2);
      expect(mid.json.body.text).toBe(winnerId === 'client-A' ? 'A-write' : 'B-write');

      // Recovery: loser refreshes the validator and retries.
      const recovered = await putJson(base, '/resources/doc', { text: 'B-write-final' }, {
        'If-Match': mid.etag, 'X-Request-Id': `${loserId}-retry`
      });
      expect(recovered.status).toBe(200);
      expect(recovered.versionHeader).toBe('3');

      // Gapless history and final state: no lost update.
      const history = (await fetch(`${base}/resources/doc/versions`).then((r) => r.json())) as {
        versions: Array<{ version: number }>;
      };
      expect(history.versions.map((v) => v.version)).toEqual([1, 2, 3]);
      const fin = await getJson(base, '/resources/doc');
      expect(fin.json.version).toBe(3);
      expect(fin.json.body).toEqual({ text: 'B-write-final' });
      expect(fin.etag).toBe(recovered.etag);

      // Both run identities are distinguishable in the decision logs.
      const runIds = new Set(env.logs.entries().map((e) => e.runId));
      expect(runIds.has('client-A')).toBe(true);
      expect(runIds.has('client-B')).toBe(true);
    } finally {
      await env.app.close();
      env.cleanup();
    }
  }, 30_000);
});
