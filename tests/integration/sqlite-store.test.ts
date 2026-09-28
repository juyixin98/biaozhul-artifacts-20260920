import { describe, expect, it, afterEach } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { SqliteResourceStore } from '../../src/state/sqlite-store.js';
import { independentETag } from '../helpers/oracle.js';
import { etagFor } from '../../src/core/versioning.js';

function tempDbPath(): string {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'rv-db-'));
  return path.join(dir, 'store.db');
}

describe('SqliteResourceStore', () => {
  let stores: SqliteResourceStore[] = [];
  let dirs: string[] = [];

  function open(busyTimeoutMs = 5_000): { store: SqliteResourceStore; dbPath: string } {
    const dbPath = tempDbPath();
    dirs.push(path.dirname(dbPath));
    const store = new SqliteResourceStore(dbPath, { busyTimeoutMs });
    stores.push(store);
    return { store, dbPath };
  }

  afterEach(() => {
    for (const s of stores) s.close();
    for (const d of dirs) rmSync(d, { recursive: true, force: true });
    stores = [];
    dirs = [];
  });

  it('appends immutable versions and exposes history in order', () => {
    const { store } = open();
    const t0 = 1_000;
    store.transact((tx) => {
      tx.insertVersion({ resourceId: 'r', version: 1, body: { v: 1 }, createdMs: t0, deleted: false });
      tx.insertVersion({ resourceId: 'r', version: 2, body: { v: 2 }, createdMs: t0 + 1, deleted: false });
    });
    const versions = store.listVersions('r');
    expect(versions.map((v) => v.version)).toEqual([1, 2]);
    expect(store.transact((tx) => tx.selectCurrent('r'))?.version).toBe(2);
    expect(store.selectVersion('r', 1)?.body).toEqual({ v: 1 });
  });

  it('treats the latest tombstone as absent but keeps counting max version', () => {
    const { store } = open();
    store.transact((tx) => {
      tx.insertVersion({ resourceId: 'r', version: 1, body: { v: 1 }, createdMs: 1, deleted: false });
      tx.insertVersion({ resourceId: 'r', version: 2, body: null, createdMs: 2, deleted: true });
    });
    expect(store.transact((tx) => tx.selectCurrent('r'))).toBeNull();
    expect(store.transact((tx) => tx.selectMaxVersion('r'))).toBe(2);
  });

  it('persists across a reopened connection', () => {
    const { store, dbPath } = open();
    const body = { name: 'widget', count: 1, tags: ['x'] };
    store.transact((tx) => {
      tx.insertVersion({ resourceId: 'r', version: 1, body, createdMs: 5, deleted: false });
    });
    store.close();

    const reopened = new SqliteResourceStore(dbPath);
    stores.push(reopened);
    const current = reopened.transact((tx) => tx.selectCurrent('r'))!;
    expect(current.version).toBe(1);
    expect(current.body).toEqual(body);
    // ETag derived from persisted bytes agrees with the independent oracle.
    expect(`"${etagFor(current.version, current.body).opaque}"`).toBe(independentETag(1, body));
  });

  it('listCurrent pages live resources only', () => {
    const { store } = open();
    store.transact((tx) => {
      tx.insertVersion({ resourceId: 'a', version: 1, body: 1, createdMs: 1, deleted: false });
      tx.insertVersion({ resourceId: 'b', version: 1, body: 2, createdMs: 1, deleted: false });
      tx.insertVersion({ resourceId: 'b', version: 2, body: null, createdMs: 2, deleted: true });
      tx.insertVersion({ resourceId: 'c', version: 1, body: 3, createdMs: 1, deleted: false });
    });
    expect(store.listCurrent(50, 0).map((r) => r.resourceId)).toEqual(['a', 'c']);
    expect(store.listCurrent(1, 1).map((r) => r.resourceId)).toEqual(['c']);
  });
});
