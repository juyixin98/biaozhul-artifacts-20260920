import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseQuery } from '../src/query/parser.js';
import { validateQuery } from '../src/query/validator.js';
import { estimateCost } from '../src/cost/estimator.js';
import { executePlan } from '../src/kernel/executor.js';
import { appSchema } from '../src/fixtures/schemaDef.js';
import { createSqliteAdapter } from '../src/state/sqliteAdapter.js';
import {
  FRAG2_ACTUAL,
  S2_ACTUAL,
  S2_ESTIMATED,
  S4_ESTIMATED,
  expectedFirstUserS1,
  s1ActualCost,
  s4ActualCost,
} from './helpers/reference.js';
import { findNode } from './helpers/harness.js';

function run(text: string, budget: number, variables: Record<string, unknown> = {}) {
  const schema = appSchema();
  const adapter = createSqliteAdapter();
  const plan = validateQuery(schema, parseQuery(text, 'unit-kernel'), variables, 'unit-kernel');
  const est = estimateCost(plan);
  const out = executePlan(plan, adapter, budget);
  adapter.close();
  return { est, out };
}

test('kernel: 预算充足时完整执行，实际成本与独立参考答案一致（S1=1765）且数据形状逐字段吻合', () => {
  // Arrange
  const text = `{ users { id name posts { id title tags { name weight } } } }`;
  // Act
  const { out } = run(text, 1_000_000);
  // Assert
  assert.equal(out.status, 'COMPLETE');
  assert.equal(out.actualCost, 1765);
  assert.equal(out.actualCost, s1ActualCost());
  assert.equal(out.costTree.total, 1765, '成本树合计必须等于实际扣减');
  const users = out.data.users as unknown[];
  assert.equal(users.length, 12);
  assert.deepEqual(users[0], expectedFirstUserS1());
});

test('kernel: S4 估计 921 低于实际 1609 —— 预算 1200 下静态放行但运行时 PARTIAL', () => {
  // Arrange
  const text = `{ users { id posts { id tags { name weight } } } }`;
  // Act
  const { est, out } = run(text, 1200);
  // Assert
  assert.equal(est.total, 921);
  assert.equal(S4_ESTIMATED, 921);
  assert.equal(s4ActualCost(), 1609);
  assert.equal(out.status, 'PARTIAL');
  assert.ok(out.actualCost <= 1200, '已扣减成本不得超过预算');
  assert.ok(out.cancellation, '必须标注取消信息');
  assert.match(out.cancellation!.path, /^\$\.users/);
});

test('kernel: 取消传播 —— 深层取消后，外层列表保留已完成前缀，未完成元素标注 incomplete', () => {
  // Arrange：预算足够完成前若干 user，但在深层 tags 元素处耗尽
  const text = `{ users { id posts { id tags { name weight } } } }`;
  // Act
  const { out } = run(text, 300);
  // Assert
  assert.equal(out.status, 'PARTIAL');
  const usersArr = out.data.users as unknown[];
  assert.ok(usersArr.length >= 1 && usersArr.length < 12, '保留部分用户前缀');
  // 每个保留的用户要么帖子完整，要么最后一个帖子带部分标签（被取消）
  const lastUser = usersArr[usersArr.length - 1] as { posts: unknown[] };
  assert.ok(lastUser.posts.length <= 6);

  const incomplete = collectIncomplete(out.costTree);
  assert.ok(incomplete.length >= 1, '成本树必须标注至少一个未完成节点');
  assert.ok(incomplete[0]!.incomplete === true);
});

test('kernel: 同字段重复别名两处都计费 —— 预算 105 在第 12 个用户的重复 role 处取消', () => {
  // Arrange
  const text = `{ users { id role role } }`;
  // Act
  const { est, out } = run(text, 105);
  // Assert
  assert.equal(est.total, S2_ESTIMATED); // 91（上界 10 时静态放行）
  assert.equal(S2_ACTUAL, 109);
  assert.equal(out.status, 'PARTIAL');
  // users 1 + 11 完整用户×9 = 100；第 12 用户 id 扣到 101，第一个 role 扣到 105；
  // 第二个重复 role 需要再扣 4 → 超预算。
  assert.equal(out.actualCost, 105);
  const usersArr = out.data.users as unknown[];
  assert.equal(usersArr.length, 11, '第 12 个用户未完成，不进入数据');
  // 成本树里能看到被取消的第二次 role（dup2 路径）
  const dupRole = findNode(out.costTree, '$.users.11.role#dup2');
  assert.equal(dupRole, undefined, '未完成字段在扣减前已抛错，树中不存在该节点');
  const firstRole = findNode(out.costTree, '$.users.11.role');
  assert.equal(firstRole?.selfCost, 4);
});

test('kernel: 重复片段展开两次都独立计费，重复不能绕过预算', () => {
  // Arrange
  const text = `{ users { ...UserCore ...UserCore } }`;
  const { est, out } = run(text, 1_000_000);
  // Assert
  assert.equal(est.total, 121);
  assert.equal(out.actualCost, FRAG2_ACTUAL); // 145
  // 收紧预算：users 1；每个用户两次展开共 12（6×2）。
  // 1+6×12=73；第 7 个用户第一次展开 id 扣到 74，随后 name 需再扣 1 → 超限。
  const tight = run(text, 74);
  assert.equal(tight.out.status, 'PARTIAL');
  assert.equal(tight.out.actualCost, 74, '第二次展开逐字段继续扣减直至超限');
  const usersArr = tight.out.data.users as unknown[];
  assert.equal(usersArr.length, 6, '第 7 个用户未完成，不进入数据');
  // 已完成元素各含两个独立展开节点
  const completedElem = findNode(tight.out.costTree, '$.users.5');
  assert.equal(completedElem?.children.filter((c) => c.field === '...UserCore').length, 2);
  // 未完成元素只来得及产生第一个展开节点，且被标注 incomplete
  const incompleteElem = findNode(tight.out.costTree, '$.users.6');
  assert.equal(incompleteElem?.incomplete, true);
  assert.equal(incompleteElem?.children.filter((c) => c.field === '...UserCore').length, 1);
});

test('kernel: 预算为 0 时第一个字段即取消，data 为空对象', () => {
  const { out } = run(`{ users { id } }`, 0);
  assert.equal(out.status, 'PARTIAL');
  assert.equal(out.actualCost, 0);
  assert.deepEqual(out.data, {});
  assert.equal(out.cancellation?.path, '$.users');
});

test('kernel: 逐元素扣减 —— 已完成元素数与卡片 actual 一致', () => {
  const { out } = run(`{ users { id } }`, 1_000_000);
  const usersNode = findNode(out.costTree, '$.users');
  assert.deepEqual(usersNode?.cardinality, { declared: 10, actual: 12 });
});

function collectIncomplete(node: { incomplete?: boolean; children: typeof node[] }): typeof node[] {
  return [
    ...(node.incomplete ? [node] : []),
    ...node.children.flatMap(collectIncomplete),
  ];
}
