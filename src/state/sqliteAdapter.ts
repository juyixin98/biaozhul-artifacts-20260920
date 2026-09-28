/**
 * SQLite 状态适配实现（node:sqlite，零外部账号）。
 *
 * 数据完全来自本地合成夹具；列表用迭代器逐行产出，
 * 执行器因此可以在任意元素后停止拉取（取消传播）。
 *
 * 缺表/缺列/约束等底层问题统一转 COMPUTATION_FAILED（由上层归类），
 * 适配器自身只抛 AdapterError。
 */
import { DatabaseSync } from 'node:sqlite';
import { FIXTURE_POSTS, FIXTURE_TAGS, FIXTURE_USERS } from '../fixtures/data.js';
import { AdapterError, type ResolveContext, type Row, type StateAdapter } from './adapter.js';

export interface SqliteAdapterOptions {
  /** ':memory:' 或文件路径 */
  location?: string;
}

export function createSqliteAdapter(opts: SqliteAdapterOptions = {}): SqliteAdapter {
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(opts.location ?? ':memory:');
  } catch (err) {
    throw new AdapterError(`failed to open sqlite at ${opts.location ?? ':memory:'}`, err);
  }
  db.exec(`
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      age INTEGER NOT NULL,
      role TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS posts (
      id INTEGER PRIMARY KEY,
      user_id INTEGER NOT NULL,
      title TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tags (
      id INTEGER PRIMARY KEY,
      post_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      weight INTEGER NOT NULL
    );
  `);
  seed(db);
  return new SqliteAdapter(db);
}

function seed(db: DatabaseSync): void {
  const count = db.prepare('SELECT COUNT(*) AS c FROM users').get() as { c: number };
  if (Number(count.c) > 0) return;
  const insUser = db.prepare('INSERT INTO users (id, name, age, role) VALUES (?, ?, ?, ?)');
  const insPost = db.prepare('INSERT INTO posts (id, user_id, title) VALUES (?, ?, ?)');
  const insTag = db.prepare('INSERT INTO tags (id, post_id, name, weight) VALUES (?, ?, ?, ?)');
  db.exec('BEGIN');
  try {
    for (const u of FIXTURE_USERS) insUser.run(u.id, u.name, u.age, u.role);
    for (const p of FIXTURE_POSTS) insPost.run(p.id, p.user_id, p.title);
    for (const t of FIXTURE_TAGS) insTag.run(t.id, t.post_id, t.name, t.weight);
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw new AdapterError('failed to seed synthetic fixtures', err);
  }
}

interface PreparedList {
  sql: string;
  bind: (ctx: ResolveContext) => SqlParam[];
}

type SqlParam = string | number | bigint | null;

const LIST_RESOLVERS: Record<string, PreparedList> = {
  'Query.users': {
    sql: 'SELECT id, name, age, role FROM users ORDER BY id',
    bind: () => [],
  },
  'User.posts': {
    sql: 'SELECT id, user_id, title FROM posts WHERE user_id = ? ORDER BY id',
    bind: (ctx) => [requireParentColumn(ctx, 'id')],
  },
  'Post.tags': {
    sql: 'SELECT id, post_id, name, weight FROM tags WHERE post_id = ? ORDER BY id',
    bind: (ctx) => [requireParentColumn(ctx, 'id')],
  },
};

function requireParentColumn(ctx: ResolveContext, column: string): SqlParam {
  if (!ctx.parent || !(column in ctx.parent)) {
    throw new AdapterError(`resolving ${ctx.typeName}.${ctx.field.name}: parent row lacks column ${column}`);
  }
  const v = ctx.parent[column];
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'bigint' || v === null) return v;
  throw new AdapterError(`column ${column} has unsupported bind type ${typeof v}`);
}

export class SqliteAdapter implements StateAdapter {
  private readonly stmts = new Map<string, ReturnType<DatabaseSync['prepare']>>();

  constructor(private readonly db: DatabaseSync) {}

  close(): void { this.db.close(); }

  root(): Row { return {}; }

  private prepared(key: string, sql: string) {
    let stmt = this.stmts.get(key);
    if (!stmt) {
      try {
        stmt = this.db.prepare(sql);
      } catch (err) {
        throw new AdapterError(`failed to prepare statement for ${key}`, err);
      }
      this.stmts.set(key, stmt);
    }
    return stmt;
  }

  resolveScalar(ctx: ResolveContext): unknown {
    if (!ctx.parent) throw new AdapterError(`scalar ${ctx.field.name} resolved without parent row`);
    if (!(ctx.field.name in ctx.parent)) {
      throw new AdapterError(`column ${ctx.field.name} missing in row of ${ctx.typeName}`);
    }
    return ctx.parent[ctx.field.name];
  }

  resolveObject(ctx: ResolveContext): Row | null {
    const key = `${ctx.typeName}.${ctx.field.name}`;
    if (key === 'Query.userById') {
      const id = ctx.args.id;
      if (typeof id !== 'number' || !Number.isInteger(id)) {
        throw new AdapterError(`userById(id) requires integer id, got ${JSON.stringify(id)}`);
      }
      try {
        const row = this.prepared(key, 'SELECT id, name, age, role FROM users WHERE id = ?').get(id) as Row | undefined;
        return row ? normalizeRow(row) : null;
      } catch (err) {
        throw new AdapterError(`object lookup ${key} failed`, err);
      }
    }
    throw new AdapterError(`no object resolver registered for ${key}`);
  }

  iterateList(ctx: ResolveContext): Iterable<Row> {
    const key = `${ctx.typeName}.${ctx.field.name}`;
    const resolver = LIST_RESOLVERS[key];
    if (!resolver) throw new AdapterError(`no list resolver registered for ${key}`);
    const stmt = this.prepared(key, resolver.sql);
    // 惰性游标：逐行 yield，取消时不再驱动迭代器，后续行不会被解析。
    return {
      *[Symbol.iterator]() {
        let rows: Row[];
        try {
          rows = stmt.all(...resolver.bind(ctx)) as Row[];
        } catch (err) {
          throw new AdapterError(`list iteration ${key} failed`, err);
        }
        for (const row of rows) yield normalizeRow(row);
      },
    };
  }
}

function normalizeRow(row: Row): Row {
  // node:sqlite 返回以 null 为原型的对象，拷贝为普通记录。
  return { ...row };
}
