import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/service/app.js';
import { ContractRegistry } from '../src/service/registry.js';
import { RunStore } from '../src/state/runStore.js';
import { buildScenario, HAPPY_INPUT, snapshotToken } from '../src/fixtures/scenario.js';
import type { CompositeResponse } from '../src/types.js';

let app: FastifyInstance;
let base: string;
let store: RunStore;

beforeAll(async () => {
  // Server-side injection: pricing deterministically fails only for
  // SKU-BROKEN, so the happy-path request (SKU-1) is unaffected.
  const scenario = buildScenario({ pricingFailForSku: 'SKU-BROKEN' });
  const registry = new ContractRegistry();
  registry.register(scenario.rawContract);
  store = new RunStore(':memory:');
  app = await buildApp({ registry, sources: scenario.sources, store, logger: false });
  await app.listen({ port: 0, host: '127.0.0.1' });
  const address = app.server.address();
  const port = typeof address === 'object' && address ? address.port : 0;
  base = `http://127.0.0.1:${port}`;
});

afterAll(async () => {
  await app.close();
  store.close();
});

async function postQuery(body: unknown): Promise<{ status: number; json: any }> {
  const res = await fetch(`${base}/query/orderDetails`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { status: res.status, json: await res.json() };
}

describe('HTTP composite API', () => {
  it('returns 200 with the exact assembled composite for the happy path', async () => {
    const { status, json } = await postQuery({
      params: HAPPY_INPUT,
      snapshotToken: snapshotToken('v1'),
      timeoutMs: 1000,
      runId: 'http-happy',
    });
    expect(status).toBe(200);
    const response = json as CompositeResponse;
    expect(response.runId).toBe('http-happy');
    expect(response.outcome).toBe('complete');
    expect(response.data['total']).toBe(105);
    expect(response.contract).toEqual({ name: 'orderDetails', version: 1 });
  });

  it('returns 202 for a partial composite with field-level failure categories', async () => {
    const { status, json } = await postQuery({
      params: { customerId: 'C1', sku: 'SKU-BROKEN' },
      snapshotToken: snapshotToken('v1'),
      runId: 'http-partial',
    });
    expect(status).toBe(202);
    const response = json as CompositeResponse;
    expect(response.outcome).toBe('partial');
    expect(response.failures[0]).toMatchObject({ category: 'SOURCE_FAILURE' });
  });

  it('maps a missing request param to a 400 MISSING_INPUT envelope', async () => {
    const { status, json } = await postQuery({ params: { customerId: 'C1' } });
    expect(status).toBe(400);
    expect(json['error']).toMatchObject({
      category: 'MISSING_INPUT',
      code: 'MISSING_REQUEST_PARAM',
    });
  });

  it('maps a bad timeout to a 400 input envelope', async () => {
    const { status, json } = await postQuery({ params: HAPPY_INPUT, timeoutMs: -5 });
    expect(status).toBe(400);
    expect(json['error'].category).toBe('MISSING_INPUT');
  });

  it('maps an unknown contract to 404', async () => {
    const res = await fetch(`${base}/query/nope`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ params: {} }),
    });
    const json = await res.json();
    expect(res.status).toBe(404);
    expect(json['error'].category).toBe('NOT_FOUND');
  });

  it('replays a run: 404 for unknown run, 200 + events for a known one', async () => {
    const missing = await fetch(`${base}/runs/does-not-exist`);
    expect(missing.status).toBe(404);

    const events = await fetch(`${base}/runs/http-happy/events`);
    expect(events.status).toBe(200);
    const body = (await events.json()) as { events: Array<{ seq: number; type: string }> };
    expect(body.events.length).toBeGreaterThan(10);
    expect(body.events[0]!.type).toBe('run-started');
    expect(body.events.at(-1)!.type).toBe('run-finished');

    const nodes = await (await fetch(`${base}/runs/http-happy/nodes`)).json();
    expect(nodes['nodes']).toHaveLength(5);

    const runs = await (await fetch(`${base}/runs`)).json();
    expect(runs['count']).toBeGreaterThanOrEqual(2);
  });

  it('lists the registered contract', async () => {
    const res = await fetch(`${base}/contracts`);
    const json = await res.json();
    expect(json['contracts'][0]).toMatchObject({
      name: 'orderDetails',
      version: 1,
      nodes: 5,
      fields: 10,
    });
  });

  it('reports health', async () => {
    const res = await fetch(`${base}/health`);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ status: 'ok' });
  });
});
