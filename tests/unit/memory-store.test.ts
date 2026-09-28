import { describe, expect, it } from 'vitest';
import { InMemoryResourceStore } from '../../src/state/memory-store.js';

function insert(store: InMemoryResourceStore, resourceId: string, version: number, body: unknown, deleted = false): void {
  store.transact((tx) => {
    tx.insertVersion({ resourceId, version, body, createdMs: version * 1000, deleted });
  });
}

describe('InMemoryResourceStore', () => {
  it('returns null for unknown resources and missing specific versions', () => {
    const store = new InMemoryResourceStore();
    expect(store.transact((tx) => tx.selectCurrent('ghost'))).toBeNull();
    expect(store.transact((tx) => tx.selectMaxVersion('ghost'))).toBe(0);
    expect(store.selectVersion('ghost', 1)).toBeNull();
    expect(store.listVersions('ghost')).toEqual([]);
  });

  it('returns independent frozen copies (caller cannot mutate history)', () => {
    const store = new InMemoryResourceStore();
    insert(store, 'r', 1, { nested: { v: 1 } });
    const a = store.transact((tx) => tx.selectCurrent('r'))!;
    (a.body as { nested: { v: number } }).nested.v = 999;
    const b = store.transact((tx) => tx.selectCurrent('r'))!;
    expect((b.body as { nested: { v: number } }).nested.v).toBe(1);
  });

  it('hides tombstoned resources from listCurrent but keeps history', () => {
    const store = new InMemoryResourceStore();
    insert(store, 'a', 1, 1);
    insert(store, 'b', 1, 1);
    insert(store, 'b', 2, null, true);
    expect(store.listCurrent(10, 0).map((r) => r.resourceId)).toEqual(['a']);
    expect(store.listVersions('b')).toHaveLength(2);
  });

  it('rejects a duplicate version insert', () => {
    const store = new InMemoryResourceStore();
    insert(store, 'r', 1, 1);
    expect(() => insert(store, 'r', 1, 2)).toThrow(/already exists/);
  });
});
