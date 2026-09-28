/**
 * 执行内核集成测试：预算网关、运行时扣减、取消传播、部分结果、
 * 四类失败区分、逐字段实际成本树。
 *
 * 所有预期数字按 fixtures/seed.ts 的固定夹具手工计算，参考答案不经过
 * 被测内核生成。
 */
import { describe, expect, it } from 'vitest';
import { DatabaseSync } from 'node:sqlite';
import { FIXTURE_SCHEMA } from '../src/fixtures/schema.js';
import { seedDatabase } from '../src/fixtures/seed.js';
import { SqliteEntityStore } from '../src/state/sqlite-store.js';
import { QueryEngine } from '../src/kernel/engine.js';
import { RunLogger } from '../src/diagnostics/run-log.js';
import type { EntityRecord, EntityStore, ListFilter } from '../src/state/store.js';
import type { CostNode } from '../src/contracts/cost.js';
import type { RunResult } from '../src/contracts/result.js';

function newEngine(customStore?: EntityStore): { engine: QueryEngine; logger: RunLogger } {
  const db = seedDatabase(new DatabaseSync(':memory:'));
  const store = customStore ?? new SqliteEntityStore(db);
  const logger = new RunLogger(null);
  const engine = new QueryEngine({ schema: FIXTURE_SCHEMA, store, logger });
  return { engine, logger };
}

function findNode(node: CostNode, label: string): CostNode | undefined {
  if (node.label === label) return node;
  for (const c of node.children) {
    const hit = findNode(c, label);
    if (hit) return hit;
  }
  return undefined;
}

const orgRoot = { root: 'Org', rootId: 1 };

describe('完整执行：实际成本树与扣减台账', () => {
  it('标量倍率与列表基数按夹具精确结算', async () => {
    const { engine } = newEngine();
    // name(1) + plan(2) + members(实际2)×name(1) = 5
    const result = await engine.execute(
      {
        ...orgRoot,
        fields: [
          { kind: 'field', alias: 'name', field: 'name' },
          { kind: 'field', alias: 'plan', field: 'plan' },
          {
            kind: 'list',
            alias: 'members',
            relation: 'members',
            declaredUpperBound: 2,
            children: [{ kind: 'field', alias: 'name', field: 'name' }],
          },
        ],
      },
      100,
    );

    expect(result.status).toBe('ok');
    expect(result.estimatedTotal).toBe(5);
    expect(result.consumed).toBe(5);
    expect(result.data).toEqual({
      name: 'ACME',
      plan: 'enterprise',
      members: [{ name: 'user1' }, { name: 'user2' }],
    });
    // 台账逐步可重放：4 次扣减（2 个根标量 + 2 个成员标量）。
    expect(result.ledger).toHaveLength(4);
    expect(result.ledger[0]).toMatchObject({ at: 'name', amount: 1, consumedAfter: 1, rejected: false });
    expect(result.ledger[3]).toMatchObject({ at: 'members / name', amount: 1, consumedAfter: 5 });

    const members = findNode(result.costTree!, 'list:members')!;
    expect(members.cardinality).toBe(2);
    expect(members.actualCardinality).toBe(2);
    expect(members.actualSubtree).toBe(2);
    expect(members.runtimeStatus).toBe('done');
    expect(result.costTree!.actualSubtree).toBe(5);
  });

  it('布尔列按 schema 类型从 0/1 回转', async () => {
    const { engine } = newEngine();
    const result = await engine.execute(
      {
        root: 'User',
        rootId: 1,
        fields: [{ kind: 'field', alias: 'active', field: 'active' }],
      },
      10,
    );
    expect(result.status).toBe('ok');
    expect(result.data).toEqual({ active: false });
  });

  it('过滤变量先类型校验再用于列表解析', async () => {
    const { engine } = newEngine();
    const result = await engine.execute(
      {
        ...orgRoot,
        fields: [
          {
            kind: 'list',
            alias: 'orgTasks',
            relation: 'orgTasks',
            declaredUpperBound: 6,
            args: [{ var: 'orgId', op: 'eq' }],
            children: [{ kind: 'field', alias: 'title', field: 'title' }],
          },
        ],
        vars: [{ name: 'orgId', type: 'int', value: 1 }],
      },
      100,
    );
    expect(result.status).toBe('ok');
    expect((result.data as { orgTasks: unknown[] }).orgTasks).toHaveLength(6);

    const empty = await engine.execute(
      {
        ...orgRoot,
        fields: [
          {
            kind: 'list',
            alias: 'orgTasks',
            relation: 'orgTasks',
            declaredUpperBound: 6,
            args: [{ var: 'orgId', op: 'eq' }],
            children: [{ kind: 'field', alias: 'title', field: 'title' }],
          },
        ],
        vars: [{ name: 'orgId', type: 'int', value: 999 }],
      },
      100,
    );
    expect(empty.status).toBe('ok');
    expect(empty.data).toEqual({ orgTasks: [] });
  });
});

describe('静态预算网关', () => {
  it('估计超限直接拒绝，不加载状态、不启动解析器', async () => {
    const { engine } = newEngine();
    // orgTasks bound 2，title(1)+body(3)=4/任务 => 估计 8；预算 7 拒绝。
    const result = await engine.execute(
      {
        ...orgRoot,
        fields: [
          {
            kind: 'list',
            alias: 'orgTasks',
            relation: 'orgTasks',
            declaredUpperBound: 2,
            children: [
              { kind: 'field', alias: 'title', field: 'title' },
              { kind: 'field', alias: 'body', field: 'body' },
            ],
          },
        ],
      },
      7,
    );
    expect(result.status).toBe('rejected_static');
    expect(result.estimatedTotal).toBe(8);
    expect(result.consumed).toBe(0);
    expect(result.data).toBeNull();
    expect(result.error).toMatchObject({
      category: 'RESOURCE_EXHAUSTED',
      code: 'BUDGET_EXCEEDED_STATIC',
    });
    expect(result.costTree!.runtimeStatus).toBe('pending');
  });

  it('explain 给出逐字段静态成本树，且不接触状态层', () => {
    const { engine } = newEngine();
    const explained = engine.explain(
      {
        ...orgRoot,
        fields: [
          {
            kind: 'list',
            alias: 'members',
            relation: 'members',
            declaredUpperBound: 2,
            children: [
              { kind: 'field', alias: 'name', field: 'name' },
              {
                kind: 'list',
                alias: 'tasks',
                relation: 'tasks',
                declaredUpperBound: 2,
                children: [
                  { kind: 'field', alias: 'title', field: 'title' },
                  { kind: 'field', alias: 'body', field: 'body' },
                ],
              },
            ],
          },
        ],
      },
      100,
    );
    expect(explained.accepted).toBe(true);
    expect(explained.estimatedTotal).toBe(18); // 2*1 + 2*2*(1+3)
    const tree = explained.costTree!;
    expect(findNode(tree, 'field:name')!.unitContribution).toBe(2);
    expect(findNode(tree, 'field:title')!.unitContribution).toBe(4);
    expect(findNode(tree, 'field:body')!.unitContribution).toBe(12);
  });
});

describe('运行时预算扣减与取消传播', () => {
  it('估计低于实际夹具时运行中扣停，保留前缀元素，未启动解析器标 aborted', async () => {
    const { engine } = newEngine();
    // 估计 = name(1) + orgTasks 声明上界2×title(1) + plan(2) = 5；预算 5
    // 网关放行。orgTasks 实际 6：前 4 个 title 扣到 5，第 5 个被拒，
    // 其后的根标量 plan 从未启动。
    const result: RunResult = await engine.execute(
      {
        ...orgRoot,
        fields: [
          { kind: 'field', alias: 'name', field: 'name' },
          {
            kind: 'list',
            alias: 'orgTasks',
            relation: 'orgTasks',
            declaredUpperBound: 2,
            children: [{ kind: 'field', alias: 'title', field: 'title' }],
          },
          { kind: 'field', alias: 'plan', field: 'plan' },
        ],
      },
      5,
    );

    expect(result.status).toBe('partial');
    expect(result.estimatedTotal).toBe(5);
    expect(result.consumed).toBe(5);
    expect(result.abort).toMatchObject({
      category: 'RESOURCE_EXHAUSTED',
      code: 'BUDGET_EXCEEDED_RUNTIME',
      at: ['orgTasks', 'title'],
      budget: 5,
      consumed: 5,
    });
    // 按 edge position 确定的前 4 个任务完整保留，第 5 个元素已入列但
    // 其 title 解析器未完成 -> 空对象前缀保留，第 6 个从未启动。
    expect(result.data).toEqual({
      name: 'ACME',
      orgTasks: [
        { title: 'task-1-1' },
        { title: 'task-1-2' },
        { title: 'task-2-1' },
        { title: 'task-2-2' },
        {},
      ],
    });
    // 偏乐观告警 + 未启动/未完成解析器在成本树上的标注。
    expect(result.warnings).toEqual([
      {
        code: 'DECLARED_BOUND_EXCEEDED',
        path: ['orgTasks'],
        declaredUpperBound: 2,
        actualCardinality: 6,
      },
    ]);
    const tree = result.costTree!;
    expect(findNode(tree, 'field:name')!.runtimeStatus).toBe('done');
    expect(findNode(tree, 'list:orgTasks')!.runtimeStatus).toBe('aborted');
    expect(findNode(tree, 'field:plan')!.runtimeStatus).toBe('aborted');
    // 台账最后一步是被拒绝的扣减。
    expect(result.ledger.at(-1)).toMatchObject({
      at: 'orgTasks / title',
      amount: 1,
      rejected: true,
      remaining: 0,
    });
  });

  it('短而深的列表链：取消在深层触发并向上传播，深层前缀状态保留', async () => {
    const { engine } = newEngine();
    // members 声明 1 实际 2；每成员 tasks 2；task 下 comments→replies×3，
    // 每层叶子 text 倍率 2。估计 = 1*2*4层*2 = 16；预算 20。
    // 实际：成员1 两个任务各 8 = 16；成员2 首任务深层扣到 22 时取消。
    const deepQuery = {
      ...orgRoot,
      fields: [
        {
          kind: 'list',
          alias: 'members',
          relation: 'members',
          declaredUpperBound: 1,
          children: [
            {
              kind: 'list',
              alias: 'tasks',
              relation: 'tasks',
              declaredUpperBound: 2,
              children: [
                {
                  kind: 'list',
                  alias: 'comments',
                  relation: 'taskComments',
                  declaredUpperBound: 1,
                  children: [
                    { kind: 'field', alias: 'text', field: 'text' },
                    {
                      kind: 'list',
                      alias: 'replies',
                      relation: 'replies',
                      declaredUpperBound: 1,
                      children: [
                        { kind: 'field', alias: 'text', field: 'text' },
                        {
                          kind: 'list',
                          alias: 'replies2',
                          relation: 'replies',
                          declaredUpperBound: 1,
                          children: [
                            { kind: 'field', alias: 'text', field: 'text' },
                            {
                              kind: 'list',
                              alias: 'replies3',
                              relation: 'replies',
                              declaredUpperBound: 1,
                              children: [{ kind: 'field', alias: 'text', field: 'text' }],
                            },
                          ],
                        },
                      ],
                    },
                  ],
                },
              ],
            },
          ],
        },
      ],
    };

    const result = await engine.execute(deepQuery, 20);
    expect(result.status).toBe('partial');
    expect(result.estimatedTotal).toBe(16);
    expect(result.consumed).toBe(20);
    expect(result.abort!.at).toEqual([
      'members',
      'tasks',
      'comments',
      'replies',
      'replies2',
      'text',
    ]);

    const data = result.data as {
      members: Array<{
        tasks: Array<{
          comments: Array<{
            text: string;
            replies: Array<{ text: string; replies2: unknown[] }>;
          }>;
        }>;
      }>;
    };
    // 成员1 完整（2 个任务，每个任务一条四层评论链）；成员2 已挂载但
    // 只解析到第 1 个任务的 replies2 层。
    expect(data.members).toHaveLength(2);
    expect(data.members[0]!.tasks).toHaveLength(2);
    expect(data.members[0]!.tasks[0]!.comments[0]!.replies[0]!.replies2).toEqual([
      {
        text: 'r-1-2',
        replies3: [{ text: 'r-1-3' }],
      },
    ]);
    expect(data.members[1]!.tasks).toHaveLength(1);
    // replies2 的元素已入列但其 text 解析器未完成 -> 空对象前缀保留。
    expect(data.members[1]!.tasks[0]!.comments).toEqual([
      { text: 'c-3-1', replies: [{ text: 'r-3-1', replies2: [{}] }] },
    ]);

    const tree = result.costTree!;
    expect(findNode(tree, 'list:members')!.runtimeStatus).toBe('aborted');
    expect(findNode(tree, 'list:replies')!.runtimeStatus).toBe('aborted');
    expect(findNode(tree, 'list:replies2')!.runtimeStatus).toBe('aborted');
    // replies3 在成员1 的两个任务里各完成一次（聚合基数 2），之后取消
    // 才在 replies2.text 处发生——未完成标注精确停在未启动的解析器上。
    expect(findNode(tree, 'list:replies3')!.actualCardinality).toBe(2);
    expect(findNode(tree, 'list:replies3')!.runtimeStatus).toBe('done');
  });

  it('重复片段展开在运行时逐份扣减，无法绕过预算', async () => {
    const { engine } = newEngine();
    // 两个分支都 spread taskCore(title1+body3=4/任务)：
    // members 声明1实际2→tasks2：估计 1*2*4=8；
    // orgTasks 声明2实际6：估计 2*4=8；合计估计 16，预算 17 放行。
    // 实际 members 分支 2 用户×2任务×4 = 16；orgTasks 第 1 个任务
    // title 扣 17 放行、body 再扣 3 到 20 被拒——第二份片段照价扣减。
    const result = await engine.execute(
      {
        ...orgRoot,
        fields: [
          {
            kind: 'list',
            alias: 'members',
            relation: 'members',
            declaredUpperBound: 1,
            children: [
              {
                kind: 'list',
                alias: 'tasks',
                relation: 'tasks',
                declaredUpperBound: 2,
                children: [{ kind: 'spread', fragment: 'taskCore' }],
              },
            ],
          },
          {
            kind: 'list',
            alias: 'orgTasks',
            relation: 'orgTasks',
            declaredUpperBound: 2,
            children: [{ kind: 'spread', fragment: 'taskCore' }],
          },
        ],
      },
      17,
    );
    expect(result.status).toBe('partial');
    expect(result.estimatedTotal).toBe(16);
    expect(result.consumed).toBe(17);
    expect(result.abort!.at).toEqual(['orgTasks', 'body']);
    const tree = result.costTree!;
    expect(findNode(tree, 'list:orgTasks')!.runtimeStatus).toBe('aborted');
    // members 第一份展开完整完成（2 用户×2 任务），证明第一份照价扣减。
    const data = result.data as {
      members: Array<{ tasks: Array<{ title: string; body: string }> }>;
      orgTasks: Array<{ title?: string; body?: string }>;
    };
    expect(data.members).toHaveLength(2);
    expect(data.members[0]!.tasks).toHaveLength(2);
    expect(data.orgTasks).toEqual([{ title: 'task-1-1' }]);
    // 每个展开分支各有独立成本节点（共两份 title/body），没有去重共享。
    const titleNodes: CostNode[] = [];
    const collect = (n: CostNode): void => {
      if (n.label === 'field:title') titleNodes.push(n);
      n.children.forEach(collect);
    };
    collect(tree);
    expect(titleNodes).toHaveLength(2);
  });
});

describe('失败类别可区分', () => {
  it('根对象不存在：STATE_CONFLICT / ROOT_NOT_FOUND', async () => {
    const { engine } = newEngine();
    const result = await engine.execute(
      { root: 'Org', rootId: 999, fields: [{ kind: 'field', alias: 'name', field: 'name' }] },
      100,
    );
    expect(result.status).toBe('failed');
    expect(result.error).toMatchObject({ category: 'STATE_CONFLICT', code: 'ROOT_NOT_FOUND' });
  });

  it('适配器抛错：COMPUTATION_FAILED / STORE_FAILURE', async () => {
    const broken: EntityStore = {
      async load(): Promise<EntityRecord> {
        return { id: 1, name: 'ACME', plan: 'x', heavyReport: 'y' };
      },
      async resolveList(_relation: string, _p: string, _id: number, _c: string, _f: ListFilter[]) {
        throw new Error('sqlite disk I/O error (synthetic)');
      },
      async close(): Promise<void> {},
    };
    const { engine } = newEngine(broken);
    const result = await engine.execute(
      {
        ...orgRoot,
        fields: [
          {
            kind: 'list',
            alias: 'members',
            relation: 'members',
            declaredUpperBound: 2,
            children: [{ kind: 'field', alias: 'name', field: 'name' }],
          },
        ],
      },
      100,
    );
    expect(result.status).toBe('failed');
    expect(result.error).toMatchObject({
      category: 'COMPUTATION_FAILED',
      code: 'STORE_FAILURE',
    });
    expect(result.error!.message).toContain('synthetic');
  });

  it('输入错误：RESOURCE 之外的畸形输入返回 INPUT_ERROR', async () => {
    const { engine } = newEngine();
    const result = await engine.execute({ root: 'Org', rootId: 1, fields: 'notarray' }, 100);
    expect(result.status).toBe('failed');
    expect(result.error!.category).toBe('INPUT_ERROR');
    expect(result.runId).toMatch(/^run_/);
  });
});
