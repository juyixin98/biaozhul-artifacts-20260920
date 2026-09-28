/**
 * State adapter tests: SQLite persistence, idempotency, event replay.
 */
import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { RunStore } from '../src/state/runStore.js';
import { ComposeService } from '../src/compose/service.js';
import type { AppConfig } from '../src/config/index.js';
import type { CompositeResult } from '../src/kernel/types.js';

function tempConfig(): { config: AppConfig; dir: string } {
  const dir = mkdtempSync(join(tmpdir(), 'compose-state-'));
  return {
    dir,
    config: {
      port: 0,
      host: '127.0.0.1',
      dbPath: join(dir, 'runs.sqlite'),
      logPath: join(dir, 'runs.jsonl'),
      defaultTimeoutMs: 1000,
      maxTimeoutMs: 5000,
    },
  };
}

describe('RunStore persistence', () => {
  let store: RunStore;
  const sample = {
    runId: 'run-sample-1',
    contract: 'orderSummary',
    status: 'partial',
    snapshotToken: 'snap-x',
    startedAt: 1,
    endedAt: 2,
    deadlineMs: 1000,
    data: { a: 1 },
    fields: [],
    nodes: [],
    limitations: [],
    errors: [],
  } as unknown as CompositeResult;

  before(() => {
    store = new RunStore(tempConfig().config.dbPath);
  });
  after(() => store.close());

  it('saves and reads back an identical result envelope', () => {
    store.saveRun(sample);
    const loaded = store.getRun('run-sample-1');
    assert.ok(loaded);
    assert.equal(loaded!.status, 'partial');
    assert.deepEqual(loaded!.data, { a: 1 });
    assert.equal(loaded!.snapshotToken, 'snap-x');
  });

  it('lists runs newest first', () => {
    store.saveRun({ ...sample, runId: 'run-sample-2', startedAt: 10 });
    const runs = store.listRuns();
    assert.equal(runs[0]!.runId, 'run-sample-2');
    assert.ok(runs.length >= 2);
  });

  it('returns null for unknown runs', () => {
    assert.equal(store.getRun('does-not-exist'), null);
  });
});

describe('idempotency keys', () => {
  it('replays the same run when key + fingerprint repeat', () => {
    const { config } = tempConfig();
    const store = new RunStore(config.dbPath);
    const first = store.reserveIdempotency('key-1', 'fp-A', 'run-1');
    const second = store.reserveIdempotency('key-1', 'fp-A', 'run-2');
    assert.deepEqual(first, { runId: 'run-1', replayed: false });
    assert.deepEqual(second, { runId: 'run-1', replayed: true });
    store.close();
  });

  it('raises STATE_CONFLICT when the same key arrives with a different payload', () => {
    const { config } = tempConfig();
    const store = new RunStore(config.dbPath);
    store.reserveIdempotency('key-2', 'fp-A', 'run-1');
    assert.throws(
      () => store.reserveIdempotency('key-2', 'fp-DIFFERENT', 'run-9'),
      (err: unknown) =>
        typeof err === 'object' &&
        err !== null &&
        (err as { detail?: { category?: string } }).detail?.category === 'STATE_CONFLICT' &&
        (err as { detail?: { reason?: string } }).detail?.reason === 'IDEMPOTENCY_KEY_MISMATCH',
    );
    store.close();
  });

  it('compose service replays the stored envelope for a repeated idempotency key', async () => {
    const { config } = tempConfig();
    const store = new RunStore(config.dbPath);
    const service = new ComposeService(store, config);
    const first = await service.run({
      contract: 'orderSummary',
      input: { userId: 'u-1001', sku: 'sku-1' },
      idempotencyKey: 'idem-http-1',
    });
    const second = await service.run({
      contract: 'orderSummary',
      input: { userId: 'u-1001', sku: 'sku-1' },
      idempotencyKey: 'idem-http-1',
    });
    assert.equal(first.replayed, false);
    assert.equal(second.replayed, true);
    assert.equal(second.result.runId, first.result.runId);
    store.close();
  });
});

describe('event replay', () => {
  it('persists ordered events with run id, seq, intermediate states and decisions', async () => {
    const { config } = tempConfig();
    const store = new RunStore(config.dbPath);
    const service = new ComposeService(store, config);
    const { result } = await service.run({
      contract: 'orderSummary',
      input: { userId: 'u-1001', sku: 'sku-1' },
      scenario: 'required-failure',
    });

    const events = store.getEvents(result.runId);
    assert.ok(events.length >= 5);
    assert.equal(events[0]!.runId, result.runId);
    // Seqs are monotonic.
    for (let i = 1; i < events.length; i += 1) {
      assert.equal(events[i]!.seq, events[i - 1]!.seq + 1);
    }
    assert.ok(events.some((e) => e.type === 'RUN_START'));
    assert.ok(events.some((e) => e.type === 'NODE_START'));
    const decision = events.find((e) => e.type === 'DECISION');
    assert.ok(decision, 'at least one judgment must be recorded');
    assert.ok(decision!.data?.rule);
    assert.ok(events.some((e) => e.type === 'RUN_END'));
    store.close();
  });
});
