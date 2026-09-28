/**
 * 执行内核单测：使用测试内手工构造的最小 schema/resolver，
 * 精确核验非空冒泡在类型树上的停止位置、列表元素非空与列表非空的区别。
 * 期望值为人工推导，不依赖业务夹具，也不由被测实现生成。
 */

import { describe, expect, it } from 'vitest';
import { executeOperation } from '../../src/graphql/execute.js';
import { GraphQLError } from '../../src/graphql/error.js';
import { parse } from '../../src/graphql/parser.js';
import type { FragmentMap } from '../../src/graphql/collect.js';
import {
  BUILTIN_SCALARS,
  type GraphQLObjectType,
  type GraphQLSchema,
  type FieldDef,
  type ResolverFn,
  list,
  named,
  nonNull,
} from '../../src/graphql/schema.js';
import type { Database } from 'better-sqlite3';

interface BoxData {
  leaf?: unknown;
  maybe?: unknown;
  child?: BoxData | null;
  childMaybe?: BoxData | null;
}

const boom = (): ResolverFn => () => {
  throw new GraphQLError('boom', { category: 'RESOLVER' });
};

function buildBoxSchema(store: {
  box: BoxData | null;
  boxNN: BoxData;
  listNN: Array<number | null>;
  listLoose: Array<number | null>;
  deep: Array<Array<number | null>>;
  childMaybe: BoxData | null;
  boxes: Array<BoxData | null>;
  boxesLoose: Array<BoxData | null>;
}): GraphQLSchema {
  const boxType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'Box',
    fields: new Map<string, FieldDef>([
      ['leaf', { name: 'leaf', type: nonNull(named('String')), args: new Map(), resolve: (s) => (s as BoxData).leaf }],
      ['maybe', { name: 'maybe', type: named('String'), args: new Map(), resolve: (s) => (s as BoxData).maybe }],
      ['child', {
        name: 'child', type: nonNull(named('Box')), args: new Map(),
        resolve: (s) => (s as BoxData).child ?? boom()(),
      }],
      ['childMaybe', {
        name: 'childMaybe', type: named('Box'), args: new Map(),
        resolve: (s) => (s as BoxData).childMaybe,
      }],
    ]),
  };

  const queryType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'Query',
    fields: new Map<string, FieldDef>([
      ['box', { name: 'box', type: named('Box'), args: new Map(), resolve: () => store.box }],
      ['boxNN', { name: 'boxNN', type: nonNull(named('Box')), args: new Map(), resolve: () => store.boxNN }],
      ['listNN', { name: 'listNN', type: nonNull(list(nonNull(named('Int')))), args: new Map(), resolve: () => store.listNN }],
      ['listLoose', { name: 'listLoose', type: list(nonNull(named('Int'))), args: new Map(), resolve: () => store.listLoose }],
      ['deep', {
        name: 'deep',
        type: nonNull(list(nonNull(list(nonNull(named('Int')))))),
        args: new Map(),
        resolve: () => store.deep,
      }],
      ['childMaybe', { name: 'childMaybe', type: named('Box'), args: new Map(), resolve: () => store.childMaybe }],
      ['boxes', {
        name: 'boxes',
        type: nonNull(list(nonNull(named('Box')))),
        args: new Map(),
        resolve: () => store.boxes,
      }],
      ['boxesLoose', {
        name: 'boxesLoose',
        type: list(nonNull(named('Box'))),
        args: new Map(),
        resolve: () => store.boxesLoose,
      }],
    ]),
  };

  const types = new Map<string, GraphQLObjectType>([
    ['Box', boxType],
    ['Query', queryType],
  ]);

  return {
    queryType,
    mutationType: null,
    getType: (name) => types.get(name) ?? BUILTIN_SCALARS[name],
  };
}

function fragmentsOf(source: string): FragmentMap {
  const doc = parse(source);
  const map: FragmentMap = {};
  for (const def of doc.definitions) {
    if (def.kind === 'FragmentDefinition') map[def.name.value] = def;
  }
  return map;
}

async function run(schema: GraphQLSchema, query: string) {
  const doc = parse(query);
  const op = doc.definitions[0];
  if (op?.kind !== 'OperationDefinition') throw new Error('need operation');
  return executeOperation({
    schema,
    operation: op,
    fragments: fragmentsOf(query),
    variables: {},
    context: {
      requestId: 'kernel-test',
      startedAt: 0,
      db: {} as Database,
    },
  });
}

describe('非空冒泡：对象树', () => {
  it('可空根字段 box 内部 leaf! 失败 => box=null，data 保留，路径深层定位', async () => {
    const schema = buildBoxSchema({
      box: { leaf: null }, boxNN: {}, listNN: [], listLoose: [], deep: [],
      childMaybe: null, boxes: [], boxesLoose: [],
    });
    const result = await run(schema, '{ box { leaf } }');
    expect(result.data).toEqual({ box: null });
    expect(result.errors).toHaveLength(1);
    expect(result.errors?.[0]?.path).toEqual(['box', 'leaf']);
    expect(result.errors?.[0]?.category).toBe('RESOLVER');
  });

  it('非空根字段 boxNN 内部 leaf! 失败 => data=null', async () => {
    const schema = buildBoxSchema({
      box: null, boxNN: { leaf: null }, listNN: [], listLoose: [], deep: [],
      childMaybe: null, boxes: [], boxesLoose: [],
    });
    const result = await run(schema, '{ boxNN { leaf } }');
    expect(result.data).toBeNull();
    expect(result.errors?.[0]?.path).toEqual(['boxNN', 'leaf']);
  });

  it('可空对象字段 childMaybe 内部 leaf! 失败 => 仅 childMaybe=null，兄弟保留', async () => {
    const schema = buildBoxSchema({
      box: null, boxNN: {}, listNN: [], listLoose: [], deep: [],
      childMaybe: { leaf: null }, boxes: [], boxesLoose: [],
    });
    const result = await run(
      schema,
      '{ childMaybe { leaf maybe } }',
    );
    expect(result.data).toEqual({ childMaybe: null });
    expect(result.errors?.[0]?.path).toEqual(['childMaybe', 'leaf']);
  });
});

describe('非空冒泡：列表（元素非空 vs 列表非空）', () => {
  it('[Int!]! 出现 null 元素 => data=null，错误路径带下标', async () => {
    const schema = buildBoxSchema({
      box: null, boxNN: {}, listNN: [1, null, 3], listLoose: [], deep: [],
      childMaybe: null, boxes: [], boxesLoose: [],
    });
    const result = await run(schema, '{ listNN }');
    expect(result.data).toBeNull();
    expect(result.errors?.[0]?.path).toEqual(['listNN', 1]);
  });

  it('[Int!]（列表可空）出现 null 元素 => 仅字段=null，data 保留', async () => {
    const schema = buildBoxSchema({
      box: null, boxNN: {}, listNN: [], listLoose: [1, null], deep: [],
      childMaybe: null, boxes: [], boxesLoose: [],
    });
    const result = await run(schema, '{ listLoose }');
    expect(result.data).toEqual({ listLoose: null });
    expect(result.errors?.[0]?.path).toEqual(['listLoose', 1]);
  });

  it('[[Int!]!]! 深层 [1][1]=null => data=null，路径两层下标', async () => {
    const schema = buildBoxSchema({
      box: null, boxNN: {}, listNN: [], listLoose: [],
      deep: [[1], [2, null]],
      childMaybe: null, boxes: [], boxesLoose: [],
    });
    const result = await run(schema, '{ deep }');
    expect(result.data).toBeNull();
    expect(result.errors?.[0]?.path).toEqual(['deep', 1, 1]);
  });

  it('[Box!]! 中某元素的 leaf! 失败 => data=null', async () => {
    const schema = buildBoxSchema({
      box: null, boxNN: {}, listNN: [], listLoose: [], deep: [],
      childMaybe: null,
      boxes: [{ leaf: 'ok' }, { leaf: null }],
      boxesLoose: [],
    });
    const result = await run(schema, '{ boxes { leaf } }');
    expect(result.data).toBeNull();
    expect(result.errors?.[0]?.path).toEqual(['boxes', 1, 'leaf']);
  });

  it('[Box!]（列表可空）中某元素 leaf! 失败 => 字段=null，兄弟根字段保留', async () => {
    const schema = buildBoxSchema({
      box: null, boxNN: {}, listNN: [], listLoose: [], deep: [],
      childMaybe: null,
      boxes: [],
      boxesLoose: [{ leaf: 'ok' }, { leaf: null }],
    });
    const result = await run(schema, '{ boxesLoose { leaf } }');
    expect(result.data).toEqual({ boxesLoose: null });
    expect(result.errors?.[0]?.path).toEqual(['boxesLoose', 1, 'leaf']);
  });

  it('可空元素 [Int] 的元素错误不冒泡：元素=null，列表完整', async () => {
    // 使用业务中 users.scores: [Int] 已在集成层覆盖；此处直接验证 list 可空元素语义：
    // 列表类型 list(named('Int'))，null 元素直接得到 null 且无错误。
    const schema = buildBoxSchema({
      box: null, boxNN: {}, listNN: [], listLoose: [], deep: [],
      childMaybe: null, boxes: [], boxesLoose: [],
    });
    const nullableListType: GraphQLObjectType = {
      kind: 'OBJECT',
      name: 'Query2',
      fields: new Map<string, FieldDef>([
        ['xs', {
          name: 'xs', type: list(named('Int')), args: new Map(),
          resolve: () => [1, null, 3],
        }],
      ]),
    };
    const schema2: GraphQLSchema = {
      queryType: nullableListType,
      mutationType: null,
      getType: (name) =>
        name === 'Query2' ? nullableListType : BUILTIN_SCALARS[name],
    };
    const result = await run(schema2, '{ xs }');
    expect(result.data).toEqual({ xs: [1, null, 3] });
    expect(result.errors).toBeUndefined();
  });
});

describe('非空冒泡：解析器抛错', () => {
  it('解析器直接抛 GraphQLError 时类别与消息保留', async () => {
    const throwing: GraphQLObjectType = {
      kind: 'OBJECT',
      name: 'Query3',
      fields: new Map<string, FieldDef>([
        ['ok', { name: 'ok', type: named('String'), args: new Map(), resolve: () => 'fine' }],
        ['bad', { name: 'bad', type: named('String'), args: new Map(), resolve: boom() }],
      ]),
    };
    const schema: GraphQLSchema = {
      queryType: throwing,
      mutationType: null,
      getType: (name) => (name === 'Query3' ? throwing : BUILTIN_SCALARS[name]),
    };
    const result = await run(schema, '{ ok bad }');
    expect(result.data).toEqual({ ok: 'fine', bad: null });
    expect(result.errors?.[0]?.message).toBe('boom');
    expect(result.errors?.[0]?.path).toEqual(['bad']);
    expect(result.errors?.[0]?.category).toBe('RESOLVER');
  });
});
