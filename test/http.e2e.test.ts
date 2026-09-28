/**
 * End-to-end HTTP tests against the real Fastify app via in-process injection
 * (no open port). Uses a throwaway SQLite file per test.
 */
import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { buildApp, type BuiltApp } from '../src/http/app.js';

let built: BuiltApp;
let dbDir: string;

before(async () => {
  dbDir = mkdtempSync(join(tmpdir(), 'compose-http-'));
  built = await buildApp({
    dbPath: join(dbDir, 'runs.sqlite'),
    logPath: join(dbDir, 'runs.jsonl'),
    defaultTimeoutMs: 1000,
    maxTimeoutMs: 5000,
  });
});

after(async () => {
  await built.app.close();
  built.store.close();
});

describe('POST /v1/compose', () => {
  it('returns a complete typed envelope for the healthy diamond', async () => {
    const reply = await built.app.inject({
      method: 'POST',
      url: '/v1/compose',
      payload: { contract: 'orderSummary', input: { userId: 'u-1001', sku: 'sku-1' } },
    });
    assert.equal(reply.statusCode, 200);
    assert.match(reply.headers['x-run-id'] as string, /^run-/);
    const body = reply.json();
    assert.equal(body.run.status, 'complete');
    assert.equal(body.run.data.customerName, 'Ada Lin');
    assert.equal(body.run.data.finalPrice, 1104.15);
    assert.equal(body.run.contract, 'orderSummary');
    assert.ok(Array.isArray(body.run.fields));
    assert.ok(body.run.limitations.some((l: { type: string }) => l.type === 'SNAPSHOT_UNSUPPORTED'));
  });

  it('returns 400 INPUT_ERROR for missing required input', async () => {
    const reply = await built.app.inject({
      method: 'POST',
      url: '/v1/compose',
      payload: { contract: 'orderSummary', input: { userId: 'u-1001' } },
    });
    assert.equal(reply.statusCode, 400);
    const body = reply.json();
    assert.equal(body.error.category, 'INPUT_ERROR');
    assert.equal(body.error.reason, 'INPUT_MISSING_FIELD');
  });

  it('returns 400 for an unknown contract', async () => {
    const reply = await built.app.inject({
      method: 'POST',
      url: '/v1/compose',
      payload: { contract: 'nope', input: {} },
    });
    assert.equal(reply.statusCode, 400);
    assert.equal(reply.json().error.reason, 'UNKNOWN_CONTRACT');
  });

  it('returns 400 for a timeout above the configured maximum', async () => {
    const reply = await built.app.inject({
      method: 'POST',
      url: '/v1/compose',
      payload: {
        contract: 'orderSummary',
        input: { userId: 'u-1001', sku: 'sku-1' },
        timeoutMs: 999999,
      },
    });
    assert.equal(reply.statusCode, 400);
    assert.equal(reply.json().error.reason, 'TIMEOUT_OUT_OF_BOUNDS');
  });

  it('partial optional-timeout is a 200 envelope carrying field-level reasons', async () => {
    const reply = await built.app.inject({
      method: 'POST',
      url: '/v1/compose',
      payload: {
        contract: 'orderSummary',
        input: { userId: 'u-1001', sku: 'sku-1' },
        scenario: 'optional-timeout',
        timeoutMs: 60,
      },
    });
    assert.equal(reply.statusCode, 200);
    const body = reply.json();
    assert.equal(body.run.status, 'partial');
    const promo = body.run.fields.find((f: { field: string }) => f.field === 'promotionCode');
    assert.equal(promo.state, 'failed');
    assert.equal(promo.requirement, 'optional');
    assert.equal(promo.reason.category, 'RESOURCE_EXHAUSTED');
    // Required values still present.
    assert.equal(body.run.data.finalPrice, 1104.15);
  });

  it('required-failure is a 200 envelope with status failed and skipped nodes', async () => {
    const reply = await built.app.inject({
      method: 'POST',
      url: '/v1/compose',
      payload: {
        contract: 'orderSummary',
        input: { userId: 'u-1001', sku: 'sku-1' },
        scenario: 'required-failure',
      },
    });
    assert.equal(reply.statusCode, 200);
    const body = reply.json();
    assert.equal(body.run.status, 'failed');
    assert.equal(body.run.errors[0].reason, 'CATALOG_UNAVAILABLE');
    assert.equal(body.run.errors[0].category, 'COMPUTATION_FAILED');
    assert.deepEqual(
      body.run.nodes
        .filter((n: { state: string }) => n.state === 'skipped')
        .map((n: { callId: string }) => n.callId)
        .sort(),
      ['promo', 'quote', 'stock', 'upsell'],
    );
  });

  it('409 STATE_CONFLICT when an idempotency key is reused with a different payload', async () => {
    const first = await built.app.inject({
      method: 'POST',
      url: '/v1/compose',
      payload: {
        contract: 'orderSummary',
        input: { userId: 'u-1001', sku: 'sku-1' },
        idempotencyKey: 'conflict-key-1',
      },
    });
    assert.equal(first.statusCode, 200);
    const second = await built.app.inject({
      method: 'POST',
      url: '/v1/compose',
      payload: {
        contract: 'orderSummary',
        input: { userId: 'u-1002', sku: 'sku-2' },
        idempotencyKey: 'conflict-key-1',
      },
    });
    assert.equal(second.statusCode, 409);
    assert.equal(second.json().error.category, 'STATE_CONFLICT');
    assert.equal(second.json().error.reason, 'IDEMPOTENCY_KEY_MISMATCH');
  });

  it('replays the identical idempotent request and marks the header', async () => {
    const payload = {
      contract: 'orderSummary',
      input: { userId: 'u-1001', sku: 'sku-1' },
      idempotencyKey: 'replay-key-1',
    };
    const first = await built.app.inject({ method: 'POST', url: '/v1/compose', payload });
    const second = await built.app.inject({ method: 'POST', url: '/v1/compose', payload });
    assert.equal(second.statusCode, 200);
    assert.equal(second.headers['idempotency-replayed'], 'true');
    assert.equal(second.json().run.runId, first.json().run.runId);
  });
});

describe('diagnostics', () => {
  it('exposes health, contracts and scenarios', async () => {
    assert.equal((await built.app.inject({ method: 'GET', url: '/health' })).json().status, 'ok');
    const contracts = (await built.app.inject({ method: 'GET', url: '/contracts' })).json();
    assert.equal(contracts.contracts[0].name, 'orderSummary');
    const fields = contracts.contracts[0].nodes
      .find((n: { id: string }) => n.id === 'quote')
      .fields.map((f: { output: string }) => f.output);
    assert.deepEqual(fields, ['finalPrice', 'discountPct']);
    const scenarios = (await built.app.inject({ method: 'GET', url: '/scenarios' })).json();
    assert.equal(scenarios.scenarios.length, 4);
  });

  it('lists runs and serves a stored run plus its replayable events', async () => {
    const created = await built.app.inject({
      method: 'POST',
      url: '/v1/compose',
      payload: { contract: 'orderSummary', input: { userId: 'u-1001', sku: 'sku-1' } },
    });
    const runId = created.json().run.runId;

    const list = (await built.app.inject({ method: 'GET', url: '/runs' })).json();
    assert.ok(list.runs.some((r: { runId: string }) => r.runId === runId));

    const detail = await built.app.inject({ method: 'GET', url: `/runs/${runId}` });
    assert.equal(detail.statusCode, 200);
    assert.equal(detail.json().runId, runId);

    const events = await built.app.inject({ method: 'GET', url: `/runs/${runId}/events` });
    assert.equal(events.statusCode, 200);
    const eventBody = events.json();
    assert.ok(eventBody.count > 5);
    assert.equal(eventBody.events[0].type, 'REQUEST_ACCEPTED');
    assert.ok(eventBody.events.some((e: { type: string }) => e.type === 'DECISION'));

    const missing = await built.app.inject({ method: 'GET', url: '/runs/no-such-run' });
    assert.equal(missing.statusCode, 404);
    assert.equal(missing.json().error.reason, 'RUN_NOT_FOUND');
  });
});
