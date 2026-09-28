/**
 * SQLite 状态适配器（node:sqlite，Node 22.5+ 内置）。
 *
 * 存储约定（全部由本地合成夹具创建）：
 * - 每个对象类型一张表，表名即类型名（Org/User/Task/Comment）；
 * - 所有关系共用一张 edge(relation, parent_id, position, child_id)，
 *   position 保证列表顺序确定，使“取消时保留了哪些前序元素”可断言；
 * - 布尔以 0/1 存储，由执行内核按 schema 类型回转。
 *
 * 参数全部用绑定传入（?），不存在字符串拼接 SQL。
 */
import { DatabaseSync } from 'node:sqlite';
import type { EntityRecord, EntityStore, ListFilter } from './store.js';

export class SqliteEntityStore implements EntityStore {
  constructor(private readonly db: DatabaseSync) {}

  static open(path: string): SqliteEntityStore {
    return new SqliteEntityStore(new DatabaseSync(path));
  }

  async load(typeName: string, id: number): Promise<EntityRecord | null> {
    // 表名来自受信 schema（非用户输入）；值以绑定参数传入。
    const stmt = this.db.prepare(`SELECT * FROM "${typeName}" WHERE id = ?`);
    const row = stmt.get(id) as EntityRecord | undefined;
    return row ? { ...row } : null;
  }

  async resolveList(
    relation: string,
    _parentType: string,
    parentId: number,
    childType: string,
    filters: ListFilter[],
  ): Promise<EntityRecord[]> {
    // 先按关系与 position 取子 id，再在子表上按列过滤。
    const edgeStmt = this.db.prepare(
      `SELECT child_id FROM edge WHERE relation = ? AND parent_id = ? ORDER BY position ASC`,
    );
    const edges = edgeStmt.all(relation, parentId) as Array<{ child_id: number }>;
    if (edges.length === 0) return [];

    const ids = edges.map((e) => e.child_id);
    const placeholders = ids.map(() => '?').join(', ');
    const where = [`id IN (${placeholders})`];
    const params: ScalarBinding[] = [...ids];
    for (const f of filters) {
      where.push(`"${f.column}" = ?`);
      params.push(toBinding(f.value));
    }
    const rows = this.db
      .prepare(`SELECT * FROM "${childType}" WHERE ${where.join(' AND ')}`)
      .all(...params) as EntityRecord[];
    // 以 edge 的 position 顺序输出（IN 不保证顺序）。
    const byId = new Map(rows.map((r) => [r.id, r]));
    const ordered: EntityRecord[] = [];
    for (const childId of ids) {
      const row = byId.get(childId);
      if (row) ordered.push({ ...row });
    }
    return ordered;
  }

  async close(): Promise<void> {
    this.db.close();
  }
}

type ScalarBinding = string | number | bigint | null;

function toBinding(v: string | number | boolean): ScalarBinding {
  if (typeof v === 'boolean') return v ? 1 : 0;
  return v;
}
