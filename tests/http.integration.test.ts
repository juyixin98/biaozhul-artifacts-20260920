import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ContractStore } from '../src/state/store.js';
import { DiffService } from '../src/diagnostics/service.js';
import { buildServer } from '../src/diagnostics/http.js';
import { FIXTURES } from './fixtures/contracts.js';
import type { FastifyInstance } from 'fastify';

describe('SQLite state adapter', () => {
  let store: ContractStore;
  beforeEach(() => {
    store = new ContractStore(':memory:');
  });
  afterEach(() => store.close());

  it('persists analysis, findings and per-request processing logs', () => {
    const service = new DiffService(store);
    const out = service.run(
      { oldContract: FIXTURES.OLD, newContract: FIXTURES.NEW, contractRef: 'pets', oldVersionLabel: '1', newVersionLabel: '2' },
      'req-state-1',
    );
    expect(out.analysisId).toBeTruthy();

    const stored = store.getAnalysis(out.analysisId!)!;
    expect(stored.result.compatible).toBe(false);
    expect(stored.result.requestFindings.length + stored.result.responseFindings.length)
      .toBeGreaterThan(5);

    const logs = store.logsFor('req-state-1');
    const steps = logs.map((l) => l.step);
    expect(steps).toContain('receive');
    expect(steps).toContain('parse-old');
    expect(steps).toContain('parse-new');
    expect(steps).toContain('diff');
    expect(steps).toContain('persist');
    // Every log row references the same request identity.
    expect(logs.every((l) => l.request_id === 'req-state-1')).toBe(true);
  });

  it('deduplicates identical contract versions on upsert', () => {
    const a = store.upsertContract('r', '1', 't', FIXTURES.OLD);
    const b = store.upsertContract('r', '1', 't', FIXTURES.OLD);
    expect(a.id).toBe(b.id);
  });
});

describe('DiffService diagnostics shape', () => {
  let store: ContractStore;
  beforeEach(() => {
    store = new ContractStore(':memory:');
  });
  afterEach(() => store.close());

  it('separates failures from uncertainties and reports versions + steps', () => {
    const out = new DiffService(store).run(
      { oldContract: FIXTURES.OLD, newContract: FIXTURES.NEW, oldVersionLabel: '1.0.0', newVersionLabel: '2.0.0' },
      'req-diag-1',
    );
    expect(out.status).toBe('breaking');
    expect(out.versions).toEqual({ old: '1.0.0', new: '2.0.0' });
    expect(out.failures!.length).toBeGreaterThan(0);
    expect(out.uncertainties!.length).toBeGreaterThan(0);
    expect(out.failures!.every((f) => f.severity === 'breaking')).toBe(true);
    expect(out.uncertainties!.every((f) => f.severity === 'undetermined')).toBe(true);
    // Steps expose version/handling location and are ordered.
    const parseOld = out.steps.find((s) => s.step === 'parse-old')!;
    expect(parseOld.message).toContain('OLD');
    expect(out.steps[0]!.step).toBe('receive');
  });

  it('returns a structured error (not a throw) on an invalid contract', () => {
    const bad = JSON.stringify({ openapi: '2.0', paths: {} });
    const out = new DiffService(store).run({ oldContract: bad, newContract: FIXTURES.NEW }, 'req-err-1');
    expect(out.status).toBe('error');
    expect(out.error!.code).toBe('CONTRACT_PARSE_ERROR');
    expect(out.error!.position).toBe('$.openapi');
    const logs = store.logsFor('req-err-1');
    expect(logs.some((l) => l.level === 'error')).toBe(true);
  });
});

describe('HTTP diagnostic endpoints', () => {
  let app: FastifyInstance;
  let store: ContractStore;

  beforeEach(async () => {
    store = new ContractStore(':memory:');
    app = await buildServer(store);
  });
  afterEach(async () => {
    await app.close();
    store.close();
  });

  it('POST /v1/diff correlates result via X-Request-Id and stores the analysis', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/v1/diff',
      headers: { 'x-request-id': 'corr-123', 'content-type': 'application/json' },
      payload: { oldContract: FIXTURES.OLD, newContract: FIXTURES.NEW },
    });
    expect(res.statusCode).toBe(200);
    expect(res.headers['x-request-id']).toBe('corr-123');
    const body = res.json() as { requestId: string; analysisId: string; status: string };
    expect(body.requestId).toBe('corr-123');
    expect(body.status).toBe('breaking');

    const stored = await app.inject({ method: 'GET', url: `/v1/analyses/${body.analysisId}` });
    expect(stored.statusCode).toBe(200);
    const storedBody = stored.json() as { requestId: string; result: { compatible: boolean } };
    expect(storedBody.requestId).toBe('corr-123');
    expect(storedBody.result.compatible).toBe(false);

    const logs = await app.inject({ method: 'GET', url: `/v1/analyses/${body.analysisId}/logs` });
    expect(logs.statusCode).toBe(200);
    const logBody = logs.json() as { requestId: string; logs: Array<{ step: string }> };
    expect(logBody.requestId).toBe('corr-123');
    expect(logBody.logs.map((l) => l.step)).toContain('diff');
  });

  it('rejects malformed requests with 400 and invalid contracts with 422', async () => {
    const badRequest = await app.inject({
      method: 'POST', url: '/v1/diff',
      payload: { oldContract: 123 },
    });
    expect(badRequest.statusCode).toBe(400);

    const invalidContract = await app.inject({
      method: 'POST', url: '/v1/diff',
      payload: { oldContract: 'openapi: 3.0.0\n', newContract: FIXTURES.NEW },
    });
    expect(invalidContract.statusCode).toBe(422);
    const json = invalidContract.json() as { error: { code: string; position: string } };
    expect(json.error.code).toBe('CONTRACT_PARSE_ERROR');
    expect(json.error.position).toBe('$.openapi');
  });

  it('GET /v1/analyses/:id returns 404 for unknown ids', async () => {
    const res = await app.inject({ method: 'GET', url: '/v1/analyses/nope' });
    expect(res.statusCode).toBe(404);
  });

  it('GET /healthz reports ok', async () => {
    const res = await app.inject({ method: 'GET', url: '/healthz' });
    expect(res.json()).toEqual({ status: 'ok' });
  });
});
