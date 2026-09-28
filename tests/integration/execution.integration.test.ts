/**
 * 集成测试：真实 SQLite + 合成夹具 + 完整 HTTP 装配。
 * 断言对照固定夹具 ID（p-fp-* 等）的具体结果与失败类别。
 */
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { buildTestApp, graphql, type RawHttpResponse } from '../fixtures/helpers.js';

const built = buildTestApp();
after(async () => {
  await built.app.close();
  built.db.close();
});

function expectOneError(res: RawHttpResponse): { message: string; path?: Array<string | number>; category: string } {
  assert.equal(res.statusCode, 200, `expected 200, got ${res.statusCode}: ${JSON.stringify(res.json)}`);
  const errors = res.json.errors as Array<{
    message: string;
    path?: Array<string | number>;
    extensions: { category: string };
  }>;
  assert.ok(errors && errors.length >= 1, 'expected at least one error');
  const first = errors[0]!;
  return { message: first.message, path: first.path, category: first.extensions.category };
}

test('基础: 对象、列表、别名、片段合并返回确定数据', async () => {
  const res = await graphql(built, {
    query: `
      fragment UserCore on User { id handle displayName }
      query Q {
        ada: user(handle: "ada") {
          ...UserCore
          friends { handle }
          posts { id title }
        }
      }`,
  });
  assert.equal(res.statusCode, 200);
  assert.equal(res.json.errors, undefined);
  const ada = res.json.data.ada;
  assert.equal(ada.id, 'u-01');
  assert.equal(ada.handle, 'ada');
  assert.deepEqual(
    ada.friends.map((f: { handle: string }) => f.handle),
    ['lin', 'noa'],
  );
  // u-01 的帖子（默认不含他人 DRAFT；自己的 p-03 属于 u-03，不在此列）
  assert.ok(ada.posts.some((p: { id: string }) => p.id === 'p-01'));
});

test('变量: ID 列表变量、枚举默认值、片段内联条件正常工作', async () => {
  const res = await graphql(built, {
    query: `query Q($ids: [ID!]) { users(ids: $ids) { id handle ... on User { emailVerified } } }`,
    variables: { ids: ['u-01', 'u-02'] },
  });
  assert.equal(res.statusCode, 200, JSON.stringify(res.json));
  assert.deepEqual(
    res.json.data.users.map((u: { id: string }) => u.id),
    ['u-01', 'u-02'],
  );
  assert.equal(res.json.data.users[0].emailVerified, true);
  assert.equal(res.json.data.users[1].emailVerified, false);
});

test('非空冒泡(元素): 非空元素为 null 令整个列表冒泡到可空 post，兄弟根字段保留', async () => {
  const res = await graphql(built, {
    query: `{
      post(id: "p-fp-element") { id related { id title } }
      other: post(id: "p-01") { id }
    }`,
  });
  const err = expectOneError(res);
  assert.equal(err.category, 'NON_NULL_VIOLATION');
  assert.deepEqual(err.path, ['post', 'related', 1]);
  // post 可空：冒泡停在 post=null；other 不受影响
  assert.deepEqual(res.json.data, { post: null, other: { id: 'p-01' } });
});

test('非空冒泡(列表本身): [Comment!]! 解析器返回 null 在字段位置冒泡', async () => {
  const res = await graphql(built, {
    query: `{ post(id: "p-fp-listnull") { id archivedComments { id } } }`,
  });
  const err = expectOneError(res);
  assert.equal(err.category, 'NON_NULL_VIOLATION');
  assert.deepEqual(err.path, ['post', 'archivedComments']);
  assert.deepEqual(res.json.data, { post: null });
});

test('非空冒泡(深层字段): Comment.body! 为 null 沿 元素->列表->Post 冒泡，路径带索引', async () => {
  const res = await graphql(built, {
    query: `{ post(id: "p-02") { id comments(includeFault: true) { id body } } }`,
  });
  const err = expectOneError(res);
  assert.equal(err.category, 'NON_NULL_VIOLATION');
  assert.deepEqual(err.path, ['post', 'comments', 1, 'body']);
  assert.deepEqual(res.json.data, { post: null });
});

test('部分数据: 可空字段解析器抛错停在本地，同对象兄弟字段照常返回', async () => {
  const res = await graphql(built, {
    query: `{ post(id: "p-fp-throw") { id body faultyNote } }`,
  });
  const err = expectOneError(res);
  assert.equal(err.category, 'RESOLVER_FAILURE');
  assert.deepEqual(err.path, ['post', 'faultyNote']);
  assert.deepEqual(res.json.data, {
    post: {
      id: 'p-fp-throw',
      body: 'Selecting title on this post throws a resolver error; body still works.',
      faultyNote: null,
    },
  });
});

test('部分数据: 非空 title 抛错冒泡整个可空 post，根级兄弟字段保留', async () => {
  const res = await graphql(built, {
    query: `{ bad: post(id: "p-fp-throw") { title } good: post(id: "p-01") { title } }`,
  });
  const err = expectOneError(res);
  assert.equal(err.category, 'RESOLVER_FAILURE');
  assert.deepEqual(err.path, ['bad', 'title']);
  assert.equal(res.json.data.bad, null);
  assert.equal(res.json.data.good.title, 'Getting started with local fixtures');
});

test('输出强制: Int 字段解析器返回字符串得到 COERCION_FAILURE（不是 500）', async () => {
  const res = await graphql(built, {
    query: `{ post(id: "p-01") { id badCount } }`,
  });
  const err = expectOneError(res);
  assert.equal(err.category, 'COERCION_FAILURE');
  assert.deepEqual(err.path, ['post', 'badCount']);
  // badCount 可空：停在字段，id 保留；post 存活
  assert.deepEqual(res.json.data.post, { id: 'p-01', badCount: null });
});

test('别名冲突: 同一响应键映射不同字段 -> 400 FIELD_CONFLICT', async () => {
  const res = await graphql(built, {
    query: `{ post(id: "p-01") { x: id x: title } }`,
  });
  assert.equal(res.statusCode, 400);
  assert.equal(res.json.data, null);
  const categories = (res.json.errors as Array<{ extensions: { category: string } }>).map(
    (e) => e.extensions.category,
  );
  assert.deepEqual(categories, ['FIELD_CONFLICT']);
});

test('片段循环: 直接自引用 -> 400 FRAGMENT_CYCLE', async () => {
  const res = await graphql(built, {
    query: `{ post(id: "p-01") { ...Self } } fragment Self on Post { id ...Self }`,
  });
  assert.equal(res.statusCode, 400);
  assert.equal(res.json.errors[0].extensions.category, 'FRAGMENT_CYCLE');
  assert.match(res.json.errors[0].message, /Self -> Self/);
});

test('片段循环: 多片段间接环 -> 400 且错误包含完整环路径', async () => {
  const res = await graphql(built, {
    query: `
      { post(id: "p-01") { ...A } }
      fragment A on Post { ...B }
      fragment B on Post { ...C }
      fragment C on Post { ...A }`,
  });
  assert.equal(res.statusCode, 400);
  assert.equal(res.json.errors[0].extensions.category, 'FRAGMENT_CYCLE');
  assert.match(res.json.errors[0].message, /A -> B -> C -> A/);
});

test('未定义片段: 400 FRAGMENT_NOT_FOUND', async () => {
  const res = await graphql(built, {
    query: `{ post(id: "p-01") { ...Nope } }`,
  });
  assert.equal(res.statusCode, 400);
  assert.equal(res.json.errors[0].extensions.category, 'FRAGMENT_NOT_FOUND');
});

test('变量校验: 必填变量缺失 -> 400 VARIABLE_TYPE，不执行', async () => {
  const res = await graphql(built, {
    query: `query Q($id: ID!) { post(id: $id) { id } }`,
  });
  assert.equal(res.statusCode, 400);
  assert.equal(res.json.data, null);
  assert.equal(res.json.errors[0].extensions.category, 'VARIABLE_TYPE');
});

test('变量校验: 布尔传给 ID 非空变量 -> 400 VARIABLE_TYPE', async () => {
  const res = await graphql(built, {
    query: `query Q($id: ID!) { post(id: $id) { id } }`,
    variables: { id: true },
  });
  assert.equal(res.statusCode, 400);
  assert.equal(res.json.errors[0].extensions.category, 'VARIABLE_TYPE');
});

test('变量校验: 非空列表变量含 null 元素 -> 400', async () => {
  const res = await graphql(built, {
    query: `query Q($ids: [ID!]) { users(ids: $ids) { id } }`,
    variables: { ids: ['u-01', null] },
  });
  assert.equal(res.statusCode, 400);
  assert.equal(res.json.errors[0].extensions.category, 'VARIABLE_TYPE');
});

test('语法错误: 花括号不闭合 -> 400 SYNTAX', async () => {
  const res = await graphql(built, { query: `{ post(id: "p-01") { id ` });
  assert.equal(res.statusCode, 400);
  assert.equal(res.json.errors[0].extensions.category, 'SYNTAX');
});

test('并发: query 两个 echoDelay 并发执行，总耗时显著小于串行', async () => {
  const started = Date.now();
  const res = await graphql(built, {
    query: `{ a: echoDelay(ms: 80, value: 11) b: echoDelay(ms: 80, value: 22) }`,
  });
  const elapsed = Date.now() - started;
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.json.data, { a: 11, b: 22 });
  assert.ok(elapsed < 150, `expected concurrent (<150ms), got ${elapsed}ms`);
});

test('顺序: mutation 顶层按文档序执行，延迟逆序时编号仍递增', async () => {
  const res = await graphql(built, {
    query: `mutation {
      c: recordPulse(label: "c", delayMs: 0)
      b: recordPulse(label: "b", delayMs: 40)
      a: recordPulse(label: "a", delayMs: 80)
    }`,
  });
  assert.equal(res.statusCode, 200, JSON.stringify(res.json));
  assert.deepEqual(res.json.data, { c: 1, b: 2, a: 3 });
});

test('顺序: 可空 mutation 抛错不阻断后续顶层字段，且保持按序', async () => {
  const res = await graphql(built, {
    query: `mutation {
      first: recordPulse(label: "first")
      fail: boom(message: "kaboom")
      last: recordPulse(label: "last")
    }`,
  });
  assert.equal(res.statusCode, 200);
  // 共享库自增序号：关键是 last 恰为 first + 1（fail 被跳过且未乱序）
  assert.equal(res.json.data.last, res.json.data.first + 1);
  assert.equal(res.json.data.fail, null);
  assert.equal(res.json.errors[0].extensions.category, 'RESOLVER_FAILURE');
  assert.deepEqual(res.json.errors[0].path, ['fail']);
});

test('状态适配: setNickname 变更经 SQLite 持久化且随后查询可见', async () => {
  const mutation = await graphql(built, {
    query: `mutation { setNickname(userId: "u-02", nickname: "lin-the-quick") { id nickname } }`,
  });
  assert.equal(mutation.statusCode, 200, JSON.stringify(mutation.json));
  assert.equal(mutation.json.data.setNickname.nickname, 'lin-the-quick');

  const query = await graphql(built, {
    query: `{ user(handle: "lin") { nickname } }`,
  });
  assert.equal(query.json.data.user.nickname, 'lin-the-quick');
});

test('状态适配: createPost 写入后出现在作者帖子列表（默认 DRAFT 需显式包含）', async () => {
  const created = await graphql(built, {
    query: `mutation {
      createPost(authorId: "u-01", title: "New integration post", body: "body", status: PUBLIC) {
        id title status
      }
    }`,
  });
  assert.equal(created.statusCode, 200, JSON.stringify(created.json));
  const newId = created.json.data.createPost.id as string;

  const list = await graphql(built, {
    query: `{ user(handle: "ada") { posts { id } } }`,
  });
  assert.ok(
    list.json.data.user.posts.some((p: { id: string }) => p.id === newId),
    'newly created post should be visible',
  );
});

test('指令: 变量驱动的 @skip 排除字段', async () => {
  const res = await graphql(built, {
    query: `query Q($hide: Boolean!) { post(id: "p-01") { id title @skip(if: $hide) } }`,
    variables: { hide: true },
  });
  assert.deepEqual(res.json.data.post, { id: 'p-01' });
});
