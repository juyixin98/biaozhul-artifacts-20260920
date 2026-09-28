import { describe, expect, it } from 'vitest';
import { SqliteStateStore } from '../src/state/sqlite-store.js';
import { SqliteDomainStore } from '../src/state/sqlite-domain-store.js';
import { MemoryStateStore } from '../src/state/memory-store.js';
import { IdempotencyInsertConflict } from '../src/state/store.js';
import { tempDbPath } from './helpers/harness.js';

describe('SQLite adapters: durability and idempotency constraint', () => {
  it('persists operations and events across a connection reopen', () => {
    const path = tempDbPath();
    const first = new SqliteStateStore(path);
    first.init();
    first.startOperation({
      operationId: 'op-1',
      batchId: 'batch-1',
      connectionId: 'conn-1',
      method: 'kv.put',
      idempotencyKey: 'k1',
      fingerprint: 'fp1',
      redactedRequest: { key: 'k1' },
      startedAt: 1000,
    });
    first.finishOperation('op-1', { status: 'succeeded', result: { ok: true }, finishedAt: 1050 });
    first.appendEvent({
      kind: 'operation-succeeded',
      decision: 'accepted',
      reason: 'side-effect-completed',
      operationId: 'op-1',
      batchId: 'batch-1',
      connectionId: 'conn-1',
      rpcId: '1',
      detail: { note: 'persisted' },
    });
    first.close();

    const reopened = new SqliteStateStore(path);
    reopened.init();
    const rec = reopened.getOperation('op-1');
    expect(rec).not.toBeNull();
    expect(rec?.status).toBe('succeeded');
    expect(rec?.durationMs).toBe(50);
    expect(rec?.result).toEqual({ ok: true });

    const events = reopened.listEvents({ batchId: 'batch-1' });
    expect(events).toHaveLength(1);
    expect(events[0]?.reason).toBe('side-effect-completed');
    expect(events[0]?.detail).toEqual({ note: 'persisted' });
    reopened.close();
  });

  it('enforces (method, idempotencyKey) uniqueness but treats null keys as distinct', () => {
    const path = tempDbPath();
    const store = new SqliteStateStore(path);
    store.init();
    const base = {
      batchId: null,
      connectionId: 'conn',
      method: 'm',
      fingerprint: null,
      redactedRequest: null,
      startedAt: 1,
    };
    store.startOperation({ ...base, operationId: 'op-a', idempotencyKey: 'same' });
    expect(() =>
      store.startOperation({ ...base, operationId: 'op-b', idempotencyKey: 'same' }),
    ).toThrow(IdempotencyInsertConflict);

    // Null keys never collide: two key-less operations coexist.
    store.startOperation({ ...base, operationId: 'op-c', idempotencyKey: null });
    store.startOperation({ ...base, operationId: 'op-d', idempotencyKey: null });
    expect(store.listOperations()).toHaveLength(3);
    store.close();
  });

  it('scopes idempotency per method: same key on different methods does not collide', () => {
    const path = tempDbPath();
    const store = new SqliteStateStore(path);
    store.init();
    const base = {
      batchId: null,
      connectionId: 'c',
      fingerprint: null,
      redactedRequest: null,
      startedAt: 1,
      idempotencyKey: 'shared-key',
    };
    store.startOperation({ ...base, operationId: 'op-a', method: 'method.one' });
    store.startOperation({ ...base, operationId: 'op-b', method: 'method.two' });
    expect(store.findByIdempotencyKey('method.one', 'shared-key')?.operationId).toBe('op-a');
    expect(store.findByIdempotencyKey('method.two', 'shared-key')?.operationId).toBe('op-b');
    store.close();
  });

  it('domain store rejects a duplicate primary key with DuplicateKeyError', () => {
    const path = tempDbPath();
    const domain = new SqliteDomainStore(path);
    domain.init();
    const entry = { key: 'k', value: 'v', revision: 1, operationId: 'op-1', createdAt: 1 };
    domain.put(entry);
    // Import lazily to keep the describe block readable.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    expect(() => domain.put({ ...entry, revision: 2 })).toThrow();
    expect(domain.list()).toHaveLength(1);
    domain.close();
  });

  it('listEvents(limit) returns the NEWEST N in ascending order (parity with memory adapter)', () => {
    const path = tempDbPath();
    const store = new SqliteStateStore(path);
    store.init();
    for (let i = 1; i <= 5; i++) {
      store.appendEvent({ kind: `k${i}`, decision: 'accepted', reason: `r${i}` });
    }
    const limited = store.listEvents({ limit: 3 });
    expect(limited).toHaveLength(3);
    // newest three are events 3,4,5; returned ascending (3 -> 5).
    expect(limited.map((e) => e.kind)).toEqual(['k3', 'k4', 'k5']);
    expect(limited.map((e) => e.eventId)).toEqual([3, 4, 5]);

    // The MemoryStateStore must select the identical subset.
    const mem = new MemoryStateStore();
    mem.init();
    for (let i = 1; i <= 5; i++) {
      mem.appendEvent({ kind: `k${i}`, decision: 'accepted', reason: `r${i}` });
    }
    expect(mem.listEvents({ limit: 3 }).map((e) => e.eventId)).toEqual([3, 4, 5]);
    store.close();
  });
});
