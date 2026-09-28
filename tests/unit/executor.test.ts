/**
 * 执行内核单元测试（非空冒泡矩阵 / 并发 / 顺序 / 片段合并 / 指令）。
 *
 * 重要：这里的 schema（Box/Item 最小模型）与解析器由测试独立搭建，
 * 期望值全部手写，不复用生产 schema 的解析器生成答案，
 * 以避免"参考答案由被测核心自身实现生成"。
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { GraphQLError } from '../../src/graphql/errors.js';
import {
  execute,
  prepareExecution,
  ValidationFailure,
} from '../../src/graphql/executor.js';
import { makeSchema, type FieldResolveInfo, type SchemaSpec } from '../../src/graphql/schema.js';

interface ItemSource {
  id: string;
  name: string;
  note?: string | null;
  failName?: boolean;
  failNote?: boolean;
  nullBang?: boolean;
  throwBang?: boolean;
}

interface BoxSource {
  id: string;
  items?: Array<ItemSource | null>;
  matrix?: Array<Array<ItemSource | null> | null>;
  nullList?: boolean;
}

interface TestContext {
  counter: number;
  order: string[];
  idCalls: number;
}

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

function schemaSpec(): SchemaSpec<TestContext> {
  return {
    objects: {
      Query: [
        { name: 'optBox', type: { kind: 'NAMED', name: 'Box', nonNull: false }, args: new Map() },
        { name: 'reqBox', type: { kind: 'NAMED', name: 'Box', nonNull: true }, args: new Map() },
        {
          name: 'brokenListBox',
          type: { kind: 'NAMED', name: 'Box', nonNull: true },
          args: new Map(),
        },
        {
          name: 'optList',
          type: {
            kind: 'LIST',
            nonNull: false,
            ofType: { kind: 'NAMED', name: 'Item', nonNull: true },
          },
          args: new Map(),
        },
        {
          name: 'reqList',
          type: {
            kind: 'LIST',
            nonNull: true,
            ofType: { kind: 'NAMED', name: 'Item', nonNull: true },
          },
          args: new Map(),
        },
        {
          name: 'nullableElemList',
          type: {
            kind: 'LIST',
            nonNull: true,
            ofType: { kind: 'NAMED', name: 'Item', nonNull: false },
          },
          args: new Map(),
        },
        {
          name: 'slow',
          type: { kind: 'NAMED', name: 'Int', nonNull: true },
          args: new Map(
            ['ms', 'value'].map((n) => [
              n,
              { name: n, type: { kind: 'NAMED', name: 'Int', nonNull: true } },
            ]),
          ),
        },
      ],
      Mutation: [
        {
          name: 'pulse',
          type: { kind: 'NAMED', name: 'Int', nonNull: true },
          args: new Map([
            ['label', { name: 'label', type: { kind: 'NAMED', name: 'String', nonNull: true } }],
            [
              'delayMs',
              {
                name: 'delayMs',
                type: { kind: 'NAMED', name: 'Int', nonNull: false },
                defaultValue: 0,
              },
            ],
          ]),
        },
      ],
      Box: [
        { name: 'id', type: { kind: 'NAMED', name: 'ID', nonNull: true }, args: new Map() },
        { name: 'optItem', type: { kind: 'NAMED', name: 'Item', nonNull: false }, args: new Map() },
        { name: 'reqItem', type: { kind: 'NAMED', name: 'Item', nonNull: true }, args: new Map() },
        {
          name: 'optList',
          type: {
            kind: 'LIST',
            nonNull: false,
            ofType: { kind: 'NAMED', name: 'Item', nonNull: true },
          },
          args: new Map(),
        },
        {
          name: 'reqList',
          type: {
            kind: 'LIST',
            nonNull: true,
            ofType: { kind: 'NAMED', name: 'Item', nonNull: true },
          },
          args: new Map(),
        },
        {
          name: 'nestedList',
          type: {
            kind: 'LIST',
            nonNull: true,
            ofType: {
              kind: 'LIST',
              nonNull: true,
              ofType: { kind: 'NAMED', name: 'Item', nonNull: true },
            },
          },
          args: new Map(),
        },
      ],
      Item: [
        { name: 'id', type: { kind: 'NAMED', name: 'ID', nonNull: true }, args: new Map() },
        { name: 'name', type: { kind: 'NAMED', name: 'String', nonNull: true }, args: new Map() },
        { name: 'note', type: { kind: 'NAMED', name: 'String', nonNull: false }, args: new Map() },
        { name: 'bang', type: { kind: 'NAMED', name: 'String', nonNull: true }, args: new Map() },
      ],
    },
    resolvers: {
      Query: {
        optBox: () => ({ id: 'opt' }) satisfies BoxSource,
        reqBox: () => ({ id: 'req' }) satisfies BoxSource,
        brokenListBox: () => ({ id: 'broken', nullList: true }) satisfies BoxSource,
        optList: () => [{ id: 'a', name: 'Alpha' }],
        reqList: () => [{ id: 'a', name: 'Alpha' }],
        nullableElemList: () => [{ id: 'a', name: 'Alpha' }],
        slow: async (_s, args) => {
          await sleep(Number(args.ms));
          return Number(args.value);
        },
      },
      Mutation: {
        pulse: async (_s, args, ctx) => {
          await sleep(Number(args.delayMs ?? 0));
          ctx.counter += 1;
          ctx.order.push(String(args.label));
          return ctx.counter;
        },
      },
      Box: {
        id: (s) => (s as BoxSource).id,
        optItem: (s) => ({ id: `${(s as BoxSource).id}-child`, name: 'Child' }),
        reqItem: (s) => ({ id: `${(s as BoxSource).id}-child`, name: 'Child' }),
        optList: (s) => (s as BoxSource).items ?? [],
        reqList: (s) => {
          const box = s as BoxSource;
          if (box.nullList) return null;
          return box.items ?? [];
        },
        nestedList: (s) => (s as BoxSource).matrix ?? [],
      },
      Item: {
        id: (s, _a, ctx, info: FieldResolveInfo) => {
          void info;
          ctx.idCalls += 1;
          return (s as ItemSource).id;
        },
        name: (s) => {
          const item = s as ItemSource;
          if (item.failName) throw new Error('forced name failure');
          return item.name;
        },
        note: (s) => {
          const item = s as ItemSource;
          if (item.failNote) throw new Error('forced note failure');
          return item.note ?? null;
        },
        bang: (s) => {
          const item = s as ItemSource;
          if (item.throwBang) throw new Error('forced bang failure');
          if (item.nullBang) return null;
          return `bang-${item.id}`;
        },
      },
    },
  };
}

function newContext(): TestContext {
  return { counter: 0, order: [], idCalls: 0 };
}

async function run(query: string, variables?: Record<string, unknown>) {
  const schema = makeSchema<TestContext>(schemaSpec());
  const ctx = newContext();
  const result = await execute(schema, ctx, query, variables ? { variables } : {});
  return { result, ctx, schema };
}

// ---- 非空冒泡矩阵 ----

test('冒泡: 健康查询返回完整对象，可空 note 为 null 停在字段', async () => {
  const { result } = await run(`{
    reqBox { id child: reqItem { id name note } }
  }`);
  assert.deepEqual(result.data, {
    reqBox: { id: 'req', child: { id: 'req-child', name: 'Child', note: null } },
  });
  assert.equal(result.errors, undefined);
});

test('冒泡: 非空元素失败使"可空列表"整体为 null，根级兄弟保留', async () => {
  const schema = makeSchema<TestContext>(schemaSpec());
  schema.resolvers.get('Query')!.set('optList', () => [
    { id: 'ok', name: 'Ok' },
    { id: 'bad', name: 'Bad', failName: true },
  ]);
  const result = await execute(schema, newContext(), `{
    optList { id name note }
    sibling: reqBox { id }
  }`);

  assert.deepEqual(result.data, { optList: null, sibling: { id: 'req' } });
  assert.equal(result.errors?.length, 1);
  assert.deepEqual(result.errors?.[0]?.path, ['optList', 1, 'name']);
  assert.equal(result.errors?.[0]?.extensions.category, 'RESOLVER_FAILURE');
});

test('冒泡: 可空列表 [Item!] 停在列表；非空列表 [Item!]! 继续冒泡到根', async () => {
  const schema = makeSchema<TestContext>(schemaSpec());
  // opt 健康；req 含故障元素。只有 req 出错却令整个 reqBox 为 null，
  // 这恰好证明 [Item!]! 的冒泡越过了列表本身。
  schema.resolvers.get('Box')!.set('optList', () => [{ id: 'ok', name: 'Ok' }]);
  schema.resolvers.get('Box')!.set('reqList', () => [
    { id: 'ok', name: 'Ok' },
    { id: 'bad', name: 'Bad', failName: true },
  ]);

  const result = await execute(
    schema,
    newContext(),
    `{ reqBox { id opt: optList { id name } req: reqList { id name } } }`,
  );

  assert.equal(result.data, null);
  assert.deepEqual((result.errors ?? []).map((e) => e.path?.join('.')), ['reqBox.req.1.name']);
});

test('冒泡: 列表本身 [Item!]! 解析器返回 null -> 在字段位置冒泡', async () => {
  const { result } = await run(`{ brokenListBox { id reqList { id } } }`);
  assert.equal(result.data, null);
  assert.deepEqual(result.errors?.[0]?.path, ['brokenListBox', 'reqList']);
  assert.equal(result.errors?.[0]?.extensions.category, 'NON_NULL_VIOLATION');
});

test('冒泡: 嵌套非空列表错误路径精确定位到 [i][j].field', async () => {
  const schema = makeSchema<TestContext>(schemaSpec());
  schema.resolvers.get('Box')!.set('nestedList', () => [
    [
      { id: 'a', name: 'A' },
      { id: 'b', name: 'B' },
    ],
    [
      { id: 'c', name: 'C' },
      { id: 'd', name: 'D', failName: true },
    ],
  ]);
  const result = await execute(schema, newContext(), `{ reqBox { nestedList { id name } } }`);

  assert.equal(result.data, null);
  assert.deepEqual(result.errors?.[0]?.path, ['reqBox', 'nestedList', 1, 1, 'name']);
  assert.ok((result.errors?.[0]?.locations?.[0]?.line ?? 0) >= 1);
});

test('冒泡: 可空元素 [Item]! 内非空失败只令该元素为 null，列表保留', async () => {
  const schema = makeSchema<TestContext>(schemaSpec());
  schema.resolvers.get('Query')!.set('nullableElemList', () => [
    { id: 'ok', name: 'Ok' },
    { id: 'bad', name: 'Bad', failName: true },
  ]);
  const result = await execute(schema, newContext(), `{ nullableElemList { id name } }`);
  assert.deepEqual(result.data, { nullableElemList: [{ id: 'ok', name: 'Ok' }, null] });
  assert.deepEqual(result.errors?.[0]?.path, ['nullableElemList', 1, 'name']);
});

test('冒泡: 可空标量解析器抛错停在字段本地，对象与兄弟字段存活', async () => {
  const schema = makeSchema<TestContext>(schemaSpec());
  schema.resolvers.get('Query')!.set('nullableElemList', () => [
    { id: 'z', name: 'Z', failNote: true },
  ]);
  const result = await execute(schema, newContext(), `{ nullableElemList { id name note } }`);
  assert.deepEqual(result.data, {
    nullableElemList: [{ id: 'z', name: 'Z', note: null }],
  });
  assert.deepEqual(result.errors?.[0]?.path, ['nullableElemList', 0, 'note']);
  assert.equal(result.errors?.[0]?.extensions.category, 'RESOLVER_FAILURE');
});

test('冒泡: 可空父对象吸收子级非空失败，其他顶层字段不丢失', async () => {
  const schema = makeSchema<TestContext>(schemaSpec());
  schema.resolvers.get('Box')!.set('reqItem', () => ({
    id: 'x',
    name: 'X',
    failName: true,
  }));
  const result = await execute(
    schema,
    newContext(),
    `{ optBox { id reqItem { id name } } safe: reqBox { id } }`,
  );
  assert.deepEqual(result.data, { optBox: null, safe: { id: 'req' } });
  assert.deepEqual(result.errors?.[0]?.path, ['optBox', 'reqItem', 'name']);
});

// ---- 并发与顺序 ----

test('并发: query 两个慢字段并发，总耗时接近 max 而非 sum', async () => {
  const started = Date.now();
  const { result } = await run(`{ a: slow(ms: 60, value: 1) b: slow(ms: 60, value: 2) }`);
  const elapsed = Date.now() - started;
  assert.deepEqual(result.data, { a: 1, b: 2 });
  assert.ok(elapsed < 110, `expected concurrent execution, got ${elapsed}ms`);
});

test('顺序: mutation 顶层严格按文档序，先出现的慢字段仍拿到最小编号', async () => {
  const { result, ctx } = await run(`mutation {
    third: pulse(label: "third", delayMs: 0)
    second: pulse(label: "second", delayMs: 30)
    first: pulse(label: "first", delayMs: 60)
  }`);
  assert.deepEqual(result.data, { third: 1, second: 2, first: 3 });
  assert.deepEqual(ctx.order, ['third', 'second', 'first']);
});

// ---- 别名 / 片段合并 / 指令 ----

test('合并: 同响应键同名字段（含片段展开）只解析一次', async () => {
  const { result, ctx } = await run(`{
    reqList { id ...A }
  }
  fragment A on Item { id name }`);
  assert.deepEqual(result.data, { reqList: [{ id: 'a', name: 'Alpha' }] });
  assert.equal(ctx.idCalls, 1);
});

test('指令: @include(if:$v=false) 与 @skip(if:true) 排除字段', async () => {
  const { result } = await run(
    `query Q($take: Boolean!) { reqBox { id @include(if: $take) hidden: id @skip(if: true) } }`,
    { take: false },
  );
  assert.deepEqual(result.data, { reqBox: {} });
});

// ---- 执行前校验类别 ----

function prepare(query: string, variables?: Record<string, unknown>) {
  return prepareExecution(makeSchema<TestContext>(schemaSpec()), query, { variables });
}

function validationCategories(query: string, variables?: Record<string, unknown>): string[] {
  try {
    prepare(query, variables);
    return [];
  } catch (err) {
    if (err instanceof ValidationFailure) return err.validationErrors.map((e) => e.category);
    if (err instanceof GraphQLError) return [err.category];
    throw err;
  }
}

test('校验: 直接片段自引用报 FRAGMENT_CYCLE', () => {
  assert.deepEqual(validationCategories(`{ reqList { ...X } } fragment X on Item { ...X }`), [
    'FRAGMENT_CYCLE',
  ]);
});

test('校验: 间接片段环报 FRAGMENT_CYCLE 且信息包含环路径', () => {
  let message = '';
  try {
    prepare(`{ reqList { ...A } }
      fragment A on Item { ...B }
      fragment B on Item { ...C }
      fragment C on Item { ...A }`);
  } catch (err) {
    assert.ok(err instanceof ValidationFailure);
    message = err.validationErrors[0]!.message;
  }
  assert.match(message, /A -> B -> C -> A/);
});

test('校验: 未定义片段报 FRAGMENT_NOT_FOUND', () => {
  assert.deepEqual(validationCategories(`{ reqList { ...Missing } }`), ['FRAGMENT_NOT_FOUND']);
});

test('校验: 同响应键不同字段名报 FIELD_CONFLICT（别名冲突）', () => {
  // x 同时映射到 id 与 name：响应键相同但底层字段不同
  assert.deepEqual(validationCategories(`{ reqList { x: id x: name } }`), [
    'FIELD_CONFLICT',
  ]);
});

test('校验: 同响应键同名字段但实参不同报 FIELD_CONFLICT', () => {
  assert.deepEqual(
    validationCategories(`{ x: slow(ms: 1, value: 1) x: slow(ms: 2, value: 2) }`),
    ['FIELD_CONFLICT'],
  );
});

test('校验: 同名字段一叶一对象报 FIELD_CONFLICT（同时伴随选择集规则错误）', () => {
  const cats = validationCategories(`{ reqBox { reqItem { id } reqItem } }`);
  assert.ok(cats.includes('FIELD_CONFLICT'), `expected FIELD_CONFLICT in ${cats.join(',')}`);
});

test('变量: 必填变量缺失执行前拒绝', () => {
  assert.deepEqual(validationCategories(`query Q($ms: Int!) { slow(ms: $ms, value: 1) }`), [
    'VARIABLE_TYPE',
  ]);
});

test('变量: 非空列表元素为 null 被拒绝且错误带索引', () => {
  let path: ReadonlyArray<string | number> | undefined;
  try {
    prepare(`query Q($v: [String!]!) { slow(ms: 1, value: 1) }`, { v: ['a', null, 'c'] });
  } catch (err) {
    assert.ok(err instanceof GraphQLError);
    path = err.path;
  }
  assert.deepEqual(path, [1]);
});

test('变量: 布尔传给 Int 执行前拒绝', () => {
  let category = '';
  try {
    prepare(`query Q($ms: Int!) { slow(ms: $ms, value: 1) }`, { ms: true });
  } catch (err) {
    assert.ok(err instanceof GraphQLError);
    category = err.category;
  }
  assert.equal(category, 'VARIABLE_TYPE');
});

test('变量: 可空变量传入非空参数位置（无默认值）被拒绝', () => {
  assert.deepEqual(
    validationCategories(`query Q($ms: Int) { slow(ms: $ms, value: 1) }`, { ms: 5 }),
    ['VARIABLE_TYPE'],
  );
});

test('错误: 非空字段解析器抛错的错误形状含 path/locations/category', async () => {
  const schema = makeSchema<TestContext>(schemaSpec());
  schema.resolvers.get('Query')!.set('nullableElemList', () => [
    { id: 'z', name: 'Z', throwBang: true },
  ]);
  const failed = await execute(schema, newContext(), `{ nullableElemList { id name bang } }`);
  const error = failed.errors?.[0]!;
  assert.deepEqual(error.path, ['nullableElemList', 0, 'bang']);
  assert.equal(error.extensions.category, 'RESOLVER_FAILURE');
  assert.ok((error.locations?.[0]?.line ?? 0) >= 1);
});
