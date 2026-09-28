/**
 * 状态适配层：SQLite 中的不可变对象仓储。
 *
 * 关键约束：字节偏移永远以“原始表示”为准——内容以未压缩 BLOB 原样存储，
 * 服务端不施加任何 Content-Encoding/压缩，因此内核给出的偏移可直接用于
 * substr(content, start+1, length)，不存在“按压缩后大小猜测”的问题。
 */
import Database from 'better-sqlite3';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import type { ObjectMeta } from '../kernel/types.js';

export interface StoredObjectInput {
  readonly id: string;
  readonly content: Buffer;
  readonly contentType: string;
  readonly lastModifiedMs: number;
  readonly etag: string;
}

interface MetaRow {
  id: string;
  size: bigint;
  etag: string;
  last_modified_ms: bigint;
  content_type: string;
}

export interface ObjectStore {
  getMeta(id: string): ObjectMeta | null;
  /** 读取闭区间 [start, end] 的原始字节；区间必须已被内核裁剪到对象范围内。 */
  readInterval(id: string, start: bigint, end: bigint): Buffer;
  close(): void;
}

const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS objects (
  id               TEXT PRIMARY KEY,
  content          BLOB NOT NULL,
  size             INTEGER NOT NULL,
  etag             TEXT NOT NULL,
  last_modified_ms INTEGER NOT NULL,
  content_type     TEXT NOT NULL
);
`;

export class SqliteObjectStore implements ObjectStore {
  private readonly db: Database.Database;

  constructor(dbPath: string) {
    if (dbPath !== ':memory:') {
      mkdirSync(dirname(dbPath), { recursive: true });
    }
    this.db = new Database(dbPath);
    // 所有整型列以 bigint 取出，避免大对象 size 越过 2^53 时丢精度。
    this.db.defaultSafeIntegers(true);
    this.db.pragma('journal_mode = WAL');
    this.db.exec(SCHEMA_SQL);
  }

  /** 插入/替换对象。仅用于种子与测试夹具；HTTP 面不暴露写入。 */
  putObject(input: StoredObjectInput): void {
    const size = BigInt(input.content.length);
    this.db
      .prepare(
        `INSERT OR REPLACE INTO objects (id, content, size, etag, last_modified_ms, content_type)
         VALUES (@id, @content, @size, @etag, @lastModifiedMs, @contentType)`,
      )
      .run({
        id: input.id,
        content: input.content,
        size,
        etag: input.etag,
        lastModifiedMs: BigInt(input.lastModifiedMs),
        contentType: input.contentType,
      });
  }

  listIds(): string[] {
    const rows = this.db.prepare('SELECT id FROM objects ORDER BY id').all() as Array<{ id: string }>;
    return rows.map((r) => r.id);
  }

  getMeta(id: string): ObjectMeta | null {
    const row = this.db
      .prepare(
        `SELECT id, size, etag, last_modified_ms, content_type
         FROM objects WHERE id = ?`,
      )
      .get(id) as MetaRow | undefined;
    if (!row) return null;
    return {
      id: row.id,
      size: row.size,
      etag: row.etag,
      lastModifiedMs: Number(row.last_modified_ms),
      contentType: row.content_type,
    };
  }

  readInterval(id: string, start: bigint, end: bigint): Buffer {
    // 物理存在的数据必然受内存限制，可安全转 Number 供 SQLite 绑定。
    const offset = Number(start) + 1; // SQLite substr 偏移从 1 开始
    const length = Number(end - start + 1n);
    const row = this.db
      .prepare('SELECT substr(content, ?, ?) AS chunk FROM objects WHERE id = ?')
      .get(offset, length, id) as { chunk: Buffer } | undefined;
    if (!row || !Buffer.isBuffer(row.chunk)) {
      throw new Error(`读取对象 ${id} 的区间失败：对象不存在`);
    }
    return row.chunk;
  }

  /** 读取完整对象（200 路径）。 */
  readAll(id: string): Buffer {
    const row = this.db
      .prepare('SELECT content FROM objects WHERE id = ?')
      .get(id) as { content: Buffer } | undefined;
    if (!row) throw new Error(`对象 ${id} 不存在`);
    return row.content;
  }

  close(): void {
    this.db.close();
  }
}
