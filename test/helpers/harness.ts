import { appSchema } from '../../src/fixtures/schemaDef.js';
import { createSqliteAdapter } from '../../src/state/sqliteAdapter.js';
import { createService, type ServiceHandle } from '../../src/diag/service.js';
import type { CostNode } from '../../src/contract/types.js';
import type { SqliteAdapter } from '../../src/state/sqliteAdapter.js';

export interface Harness {
  service: ServiceHandle;
  adapter: SqliteAdapter;
}

export function makeService(): Harness {
  const adapter = createSqliteAdapter();
  const service = createService(appSchema(), adapter);
  return { service, adapter };
}

export function findNode(root: CostNode, path: string): CostNode | undefined {
  if (root.path === path) return root;
  for (const c of root.children) {
    const hit = findNode(c, path);
    if (hit) return hit;
  }
  return undefined;
}

/** 收集树上所有字段名（含片段展开标记） */
export function fieldNames(root: CostNode): string[] {
  return [root.field, ...root.children.flatMap(fieldNames)];
}
