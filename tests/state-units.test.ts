import { describe, expect, it } from 'vitest';
import { MemoryDomainStore } from '../src/state/memory-domain-store.js';
import { DuplicateKeyError } from '../src/state/domain-store.js';
import { MemoryStateStore } from '../src/state/memory-store.js';

describe('MemoryDomainStore: duplicate and missing branches', () => {
  it('returns null for a missing key and throws on duplicate put', () => {
    const d = new MemoryDomainStore();
    d.init();
    expect(d.get('absent')).toBeNull();
    expect(d.list()).toEqual([]);
    const entry = { key: 'k', value: 'v', revision: 1, operationId: 'op-1', createdAt: 1 };
    d.put(entry);
    expect(() => d.put({ ...entry, revision: 2 })).toThrow(DuplicateKeyError);
    expect(d.get('k')?.revision).toBe(1);
    d.close();
    expect(d.list()).toEqual([]);
  });
});

describe('MemoryStateStore: lookup and listing branches', () => {
  const base = {
    batchId: 'b',
    connectionId: 'c',
    fingerprint: 'fp',
    redactedRequest: null,
    startedAt: 1,
  } as const;

  it('returns null for missing operation and key lookup', () => {
    const s = new MemoryStateStore();
    s.init();
    expect(s.getOperation('nope')).toBeNull();
    expect(s.findByIdempotencyKey('m', 'nope')).toBeNull();
  });

  it('throws on a duplicate operation id and filters listings', () => {
    const s = new MemoryStateStore();
    s.init();
    s.startOperation({ ...base, operationId: 'op-1', method: 'm', idempotencyKey: 'k' });
    expect(() =>
      s.startOperation({ ...base, operationId: 'op-1', method: 'm', idempotencyKey: 'k2' }),
    ).toThrow(/duplicate operation id/);

    s.finishOperation('op-1', { status: 'succeeded', result: { x: 1 }, finishedAt: 5 });
    expect(s.listOperations({ method: 'm' })).toHaveLength(1);
    expect(s.listOperations({ status: 'succeeded' })).toHaveLength(1);
    expect(s.listOperations({ status: 'failed' })).toHaveLength(0);
    expect(s.listOperations({ idempotencyKey: 'k' })).toHaveLength(1);
    expect(s.listOperations({ limit: 1 })).toHaveLength(1);

    const evt = s.appendEvent({
      kind: 'k',
      decision: 'accepted',
      reason: 'r',
      operationId: 'op-1',
      batchId: 'b',
      connectionId: 'c',
      rpcId: '1',
    });
    expect(evt.eventId).toBe(1);
    expect(s.listEvents({ operationId: 'op-1' })).toHaveLength(1);
    expect(s.listEvents({ connectionId: 'c' })).toHaveLength(1);
    expect(s.listEvents({ limit: 1 })).toHaveLength(1);
    expect(s.listEvents({ batchId: 'other' })).toHaveLength(0);
    s.close();
    expect(s.listOperations()).toEqual([]);
  });
});
