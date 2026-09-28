/**
 * 执行顺序核验：
 *  - query 顶层字段并发（两个延迟字段重叠执行，总耗时 < 二者之和）
 *  - mutation 顶层字段严格串行（bump 计数顺序确定）
 */

import { describe, expect, it } from 'vitest';
import { executeOperation } from '../../src/graphql/execute.js';
import { parse } from '../../src/graphql/parser.js';
import type { FragmentMap } from '../../src/graphql/collect.js';
import {
  BUILTIN_SCALARS,
  type GraphQLObjectType,
  type GraphQLSchema,
  type FieldDef,
  named,
  nonNull,
} from '../../src/graphql/schema.js';
import type { Database } from 'better-sqlite3';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function makeSchema(events: string[], counter: { value: number }): GraphQLSchema {
  const counterType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'Counter',
    fields: new Map<string, FieldDef>([
      ['value', { name: 'value', type: nonNull(named('Int')), args: new Map() }],
    ]),
  };
  const queryType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'Query',
    fields: new Map<string, FieldDef>([
      ['slow', {
        name: 'slow', type: nonNull(named('Int')), args: new Map(),
        resolve: async () => {
          events.push('slow:start');
          await sleep(30);
          events.push('slow:end');
          return 1;
        },
      }],
      ['fast', {
        name: 'fast', type: nonNull(named('Int')), args: new Map(),
        resolve: async () => {
          events.push('fast:start');
          await sleep(10);
          events.push('fast:end');
          return 2;
        },
      }],
    ]),
  };
  const mutationType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'Mutation',
    fields: new Map<string, FieldDef>([
      ['bump', {
        name: 'bump', type: nonNull(named('Counter')), args: new Map(),
        resolve: async () => {
          await sleep(5);
          counter.value += 1;
          events.push(`bump:${counter.value}`);
          return { value: counter.value };
        },
      }],
    ]),
  };
  const types = new Map<string, GraphQLObjectType>([
    ['Counter', counterType], ['Query', queryType], ['Mutation', mutationType],
  ]);
  return {
    queryType,
    mutationType,
    getType: (name) => types.get(name) ?? BUILTIN_SCALARS[name],
  };
}

function runOp(schema: GraphQLSchema, source: string) {
  const doc = parse(source);
  const op = doc.definitions[0];
  if (op?.kind !== 'OperationDefinition') throw new Error('need operation');
  const fragments: FragmentMap = {};
  return executeOperation({
    schema,
    operation: op,
    fragments,
    variables: {},
    context: { requestId: 'order', startedAt: 0, db: {} as Database },
  });
}

describe('执行顺序', () => {
  it('query 顶层字段并发：start 事件在任一 end 前都已发出', async () => {
    const events: string[] = [];
    const schema = makeSchema(events, { value: 0 });
    const started = Date.now();
    const result = await runOp(schema, '{ slow fast }');
    const elapsed = Date.now() - started;

    expect(result.data).toEqual({ slow: 1, fast: 2 });
    expect(events[0]).toMatch(/start/);
    expect(events[1]).toMatch(/start/);
    // 并发执行耗时应显著小于串行(40ms)；留足 CI 余量
    expect(elapsed).toBeLessThan(38);
  });

  it('mutation 顶层字段串行：两个 bump 按文档顺序得到 1、2', async () => {
    const events: string[] = [];
    const counter = { value: 0 };
    const schema = makeSchema(events, counter);
    const result = await runOp(schema, 'mutation { a: bump { value } b: bump { value } }');
    expect(result.data).toEqual({ a: { value: 1 }, b: { value: 2 } });
    expect(events).toEqual(['bump:1', 'bump:2']);
  });

  it('mutation 的嵌套选择字段仍并发（串行边界只在顶层）', async () => {
    const events: string[] = [];
    const inner: GraphQLObjectType = {
      kind: 'OBJECT',
      name: 'Inner',
      fields: new Map<string, FieldDef>([
        ['slow', {
          name: 'slow', type: nonNull(named('Int')), args: new Map(),
          resolve: async () => { events.push('slow:start'); await sleep(20); events.push('slow:end'); return 1; },
        }],
        ['fast', {
          name: 'fast', type: nonNull(named('Int')), args: new Map(),
          resolve: async () => { events.push('fast:start'); await sleep(5); events.push('fast:end'); return 2; },
        }],
      ]),
    };
    const mutationType: GraphQLObjectType = {
      kind: 'OBJECT',
      name: 'Mutation',
      fields: new Map<string, FieldDef>([
        ['touch', {
          name: 'touch', type: nonNull(named('Inner')), args: new Map(),
          resolve: () => ({ id: 'x' }),
        }],
      ]),
    };
    const types = new Map<string, GraphQLObjectType>([
      ['Inner', inner], ['Mutation', mutationType],
    ]);
    const schema: GraphQLSchema = {
      queryType: mutationType, // 仅为满足字段存在
      mutationType,
      getType: (name) => types.get(name) ?? BUILTIN_SCALARS[name],
    };
    const result = await runOp(schema, 'mutation { touch { slow fast } }');
    expect(result.data).toEqual({ touch: { slow: 1, fast: 2 } });
    expect(events[0]).toMatch(/start/);
    expect(events[1]).toMatch(/start/);
  });
});
