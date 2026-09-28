import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseQuery } from '../src/query/parser.js';
import { validateQuery } from '../src/query/validator.js';
import { estimateCost } from '../src/cost/estimator.js';
import { appSchema } from '../src/fixtures/schemaDef.js';
import {
  FRAG2_ESTIMATED,
  S1_ESTIMATED,
  S4_ESTIMATED,
  S5_ESTIMATED,
  REF_BOUND,
} from './helpers/reference.js';
import { findNode } from './helpers/harness.js';

const schema = appSchema();

function estimate(text: string, variables: Record<string, unknown> = {}) {
  const plan = validateQuery(schema, parseQuery(text, 'unit-estimate'), variables, 'unit-estimate');
  return estimateCost(plan);
}

test('estimator: 深列表静态成本按声明上界 10×5×4 逐层相乘，断言字面量 1031', () => {
  // S1: users{id name posts{id title tags{name weight}}}
  const { total, costTree } = estimate(`{
    users { id name posts { id title tags { name weight } } }
  }`);
  // tag 元素 name1+weight3=4 → tags=1+4×4=17
  // post 元素 id1+title2+tags17=20 → posts=1+5×20=101
  // user 元素 id1+name1+posts101=103 → users=1+10×103=1031
  assert.equal(total, 1031);
  assert.equal(S1_ESTIMATED, 1031, '独立参考答案必须与被测一致（且两边都断言字面量）');

  const users = findNode(costTree, '$.users');
  assert.ok(users);
  assert.deepEqual(users?.cardinality, { declared: REF_BOUND.users });
  const tags = findNode(costTree, '$.users.[].posts.[].tags');
  assert.equal(tags?.cardinality?.declared, REF_BOUND.tags);
});

test('estimator: S4 选择集（id/posts/tags）估计为字面量 921', () => {
  const { total } = estimate(`{ users { id posts { id tags { name weight } } } }`);
  assert.equal(total, 921);
  assert.equal(S4_ESTIMATED, 921);
});

test('estimator: S5 估计为字面量 181', () => {
  const { total } = estimate(`{ users { id name posts { id title } } }`);
  assert.equal(total, 181);
  assert.equal(S5_ESTIMATED, 181);
});

test('estimator: 片段重复展开两次 —— 每个 spread 独立一棵计费子树，不能复用/绕过', () => {
  const { total, costTree } = estimate(`{ users { ...UserCore ...UserCore } }`);
  assert.equal(total, 121);
  assert.equal(FRAG2_ESTIMATED, 121);
  const users = findNode(costTree, '$.users');
  // users 下为每元素占位节点，两个独立展开节点挂在其下
  const perElement = users?.children.find((c) => /per-element/.test(c.field));
  const spreads = perElement?.children.filter((c) => c.field === '...UserCore');
  assert.equal(spreads?.length, 2, '同一片段两次展开必须产生两个独立节点');
  assert.equal(spreads?.[0]?.total, 6);
  assert.equal(spreads?.[1]?.total, 6);
  // 每个展开点下都有完整三字段
  for (const s of spreads!) {
    assert.deepEqual(s.children.map((c) => c.field).sort(), ['id', 'name', 'role']);
  }
});

test('estimator: 同字段重复别名（id id）两处都计费', () => {
  const { total } = estimate(`{ users { id id } }`);
  // users 1 + 10 × (1 + 1) = 21
  assert.equal(total, 21);
});

test('estimator: 成本树字段倍率逐节点可核对 —— role 的 self=4 且经片段展开有 viaFragment 标记', () => {
  const { costTree } = estimate(`{ users { ...UserCore } }`);
  // 声明上界下仅展开一个“每元素”模板节点
  const role = findNode(costTree, '$.users.[].role');
  assert.equal(role?.selfCost, 4);
  assert.equal(role?.viaFragment, 'UserCore');
});

test('estimator: 查询字符数不计成本 —— 仅空白/注释差异不改变估计', () => {
  const compact = estimate(`{users{id}}`);
  const spacious = estimate(`{
    # this is a long explanatory comment that absolutely must not be billed
    users   {
      id     # trailing comment
    }
  }`);
  assert.equal(spacious.total, compact.total);
  assert.equal(compact.total, 1 + 10 * 1);
});
