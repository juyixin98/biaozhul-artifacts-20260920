import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { strongEtag } from '../../src/store/etag.js';
import { SqliteObjectStore } from '../../src/store/sqliteStore.js';

describe('SqliteObjectStore 状态适配层', () => {
  const stores: SqliteObjectStore[] = [];

  function makeStore(): SqliteObjectStore {
    const s = new SqliteObjectStore(':memory:');
    stores.push(s);
    return s;
  }

  afterEach(() => {
    while (stores.length) stores.pop()?.close();
  });

  it('写入后 getMeta 返回 bigint size 与 ETag', () => {
    const store = makeStore();
    const content = Buffer.from('0123456789', 'ascii');
    store.putObject({
      id: 'o',
      content,
      contentType: 'text/plain',
      lastModifiedMs: 1234,
      etag: strongEtag(content),
    });
    const meta = store.getMeta('o');
    expect(meta).not.toBeNull();
    expect(meta!.size).toBe(10n);
    expect(meta!.etag).toBe(strongEtag(content));
    expect(meta!.lastModifiedMs).toBe(1234);
  });

  it('readInterval 按闭区间取原始字节（偏移基于原对象，非压缩后）', () => {
    const store = makeStore();
    const content = Buffer.from('ABCDEFGHIJ', 'ascii');
    store.putObject({ id: 'o', content, contentType: 'text/plain', lastModifiedMs: 0, etag: '"e"' });
    expect(store.readInterval('o', 0n, 2n).toString('ascii')).toBe('ABC');
    expect(store.readInterval('o', 9n, 9n).toString('ascii')).toBe('J');
    expect(store.readInterval('o', 3n, 7n).toString('ascii')).toBe('DEFGH');
  });

  it('二进制内容往返逐字节一致（含高位字节，不被当文本破坏）', () => {
    const store = makeStore();
    const content = Buffer.from(Array.from({ length: 256 }, (_, i) => i));
    store.putObject({ id: 'bin', content, contentType: 'application/octet-stream', lastModifiedMs: 0, etag: '"b"' });
    const back = store.readInterval('bin', 0n, 255n);
    expect(back.equals(content)).toBe(true);
  });

  it('零长度对象：getMeta 成功，readAll 为空缓冲', () => {
    const store = makeStore();
    store.putObject({ id: 'empty', content: Buffer.alloc(0), contentType: 'application/octet-stream', lastModifiedMs: 0, etag: '"0"' });
    expect(store.getMeta('empty')!.size).toBe(0n);
    expect(store.readAll('empty').length).toBe(0);
  });

  it('不存在对象：getMeta 返回 null，readInterval/readAll 抛错', () => {
    const store = makeStore();
    expect(store.getMeta('ghost')).toBeNull();
    expect(() => store.readInterval('ghost', 0n, 1n)).toThrow();
    expect(() => store.readAll('ghost')).toThrow();
  });

  it('重新 put 同 id 覆盖（幂等种子）', () => {
    const store = makeStore();
    store.putObject({ id: 'o', content: Buffer.from('aaa'), contentType: 'text/plain', lastModifiedMs: 1, etag: '"a"' });
    store.putObject({ id: 'o', content: Buffer.from('bbbb'), contentType: 'text/plain', lastModifiedMs: 2, etag: '"b"' });
    expect(store.getMeta('o')!.size).toBe(4n);
    expect(store.readAll('o').toString()).toBe('bbbb');
  });

  it('listIds 按 id 排序返回', () => {
    const store = makeStore();
    store.putObject({ id: 'b', content: Buffer.from('b'), contentType: 'text/plain', lastModifiedMs: 0, etag: '"b"' });
    store.putObject({ id: 'a', content: Buffer.from('a'), contentType: 'text/plain', lastModifiedMs: 0, etag: '"a"' });
    expect(store.listIds()).toEqual(['a', 'b']);
  });

  it('文件型库：父目录不存在时自动创建，重开后数据持久', () => {
    const dir = mkdtempSync(join(tmpdir(), 'range-db-'));
    try {
      const dbPath = join(dir, 'nested', 'objects.db');
      const first = new SqliteObjectStore(dbPath);
      first.putObject({ id: 'p', content: Buffer.from('persist'), contentType: 'text/plain', lastModifiedMs: 0, etag: '"p"' });
      first.close();

      const reopened = new SqliteObjectStore(dbPath);
      expect(reopened.readAll('p').toString()).toBe('persist');
      reopened.close();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
