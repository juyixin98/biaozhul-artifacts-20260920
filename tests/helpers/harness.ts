/**
 * 测试公共工具：每个用例使用独立的内存 SQLite + 合成夹具播种，
 * 保证用例间互不污染（counter 顺序断言依赖初始值 0）。
 */

import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Database } from 'better-sqlite3';
import { openDatabase, seedDatabase, type SeedData } from '../../src/state/db.js';
import { buildSchema } from '../../src/state/schema.js';
import { runGraphQL, type GraphQLSchema } from '../../src/graphql/index.js';

const FIXTURES_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../fixtures',
);

let cachedSeed: SeedData | null = null;

export async function loadSeedData(): Promise<SeedData> {
  if (!cachedSeed) {
    const raw = await readFile(path.join(FIXTURES_DIR, 'seed-data.json'), 'utf8');
    cachedSeed = JSON.parse(raw) as SeedData;
  }
  return cachedSeed;
}

export interface TestHarness {
  db: Database;
  schema: GraphQLSchema;
  run: (params: {
    query: string;
    variables?: Record<string, unknown> | null;
    operationName?: string | null;
  }) => ReturnType<typeof runGraphQL>;
}

export async function createHarness(): Promise<TestHarness> {
  const seed = await loadSeedData();
  const db = openDatabase(':memory:');
  seedDatabase(db, seed);
  const schema = buildSchema();
  return {
    db,
    schema,
    run: ({ query, variables, operationName }) =>
      runGraphQL({
        schema,
        query,
        variables: variables ?? {},
        operationName: operationName ?? null,
        context: {
          requestId: `test-${Math.random().toString(36).slice(2, 10)}`,
          startedAt: Date.now(),
          db,
        },
      }),
  };
}
