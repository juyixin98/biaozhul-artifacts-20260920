/**
 * 本地演示脚本：npm run demo
 *
 * 依次保留题目要求的验证过程：
 *   A. 短而深的列表查询 —— 深层取消传播与逐字段成本树
 *   B. 重复别名 —— INPUT_ERROR / DUPLICATE_ALIAS
 *   C. 循环片段 —— INPUT_ERROR / FRAGMENT_CYCLE（不栈溢出）
 *   D. 估计低于实际夹具 —— 静态放行后运行中扣减、取消、部分结果
 *   E. 静态预算网关 —— 估计超限直接拒绝（从未执行）
 *
 * 每个场景打印运行编号、关键中间状态、判断理由；D 打印完整成本树与台账。
 */
import { buildApp } from '../src/bootstrap.js';
import { renderCostTree } from '../src/diagnostics/render-tree.js';
import type { RunResult } from '../src/contracts/result.js';

const built = buildApp({ logFile: null });
const { engine } = built;
const lines: string[] = [];
const say = (s: string): void => {
  lines.push(s);
};

const orgRoot = { root: 'Org', rootId: 1 };

function banner(title: string): void {
  say('');
  say('='.repeat(78));
  say(title);
  say('='.repeat(78));
}

function summarize(label: string, r: RunResult): void {
  say(`[${label}] runId=${r.runId}`);
  say(`  status=${r.status} estimated=${r.estimatedTotal} budget=${r.budget} consumed=${r.consumed}`);
  if (r.error) say(`  error=${r.error.category}/${r.error.code}: ${r.error.message}`);
  if (r.abort) say(`  abort at=${r.abort.at.join(' / ')} consumed=${r.abort.consumed}`);
}

async function main(): Promise<void> {
  // A. 短而深：members(声明1/实际2) → tasks(2) → comments → replies×3
  banner('A. 短而深的列表查询：估计 16，预算 20，深层取消传播');
  const deep = await engine.execute(
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
    },
    20,
  );
  summarize('A', deep);
  say('  部分结果（成员2 仅解析到 replies2 的空对象前缀）:');
  say(`    ${JSON.stringify(deep.data).slice(0, 220)}...`);
  say('  逐字段成本树（✓完成 / ✗aborted 未完成解析器）:');
  say(renderCostTree(deep.costTree!).split('\n').map((l) => `    ${l}`).join('\n'));

  // B. 重复别名
  banner('B. 重复别名：同层结果键冲突必须是输入错误');
  const dup = await engine.execute(
    {
      ...orgRoot,
      fields: [
        { kind: 'field', alias: 'n', field: 'name' },
        { kind: 'field', alias: 'n', field: 'plan' },
      ],
    },
    100,
  );
  summarize('B', dup);

  // C. 循环片段
  banner('C. 循环片段 loopA→loopB→loopA：静态展开阶段拒绝，不栈溢出');
  const cyc = await engine.execute(
    { ...orgRoot, fields: [{ kind: 'spread', fragment: 'loopA' }] },
    100,
  );
  summarize('C', cyc);
  say(`  cycle=${JSON.stringify(cyc.error?.details?.cycle)}`);

  // D. 估计低于实际夹具
  banner('D. 声明上界 2 < 实际 6：估计放行(5)，运行中第 5 个任务取消');
  const optimistic = await engine.execute(
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
  summarize('D', optimistic);
  say(`  warnings=${JSON.stringify(optimistic.warnings)}`);
  say(`  partial data=${JSON.stringify(optimistic.data)}`);
  say('  预算扣减台账（最后一步 rejected=true）:');
  for (const step of optimistic.ledger as Array<Record<string, unknown>>) {
    say(`    ${JSON.stringify(step)}`);
  }

  // E. 静态网关
  banner('E. 静态预算网关：估计 8 > 预算 7，直接拒绝且永不执行');
  const rejected = await engine.execute(
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
  summarize('E', rejected);
  say(`  consumed=${rejected.consumed}（应为 0），data=${JSON.stringify(rejected.data)}`);

  say('');
  process.stdout.write(`${lines.join('\n')}\n`);
  await built.app.close();
  built.db.close();
}

main().catch((err: unknown) => {
  process.stderr.write(`demo failed: ${err instanceof Error ? err.stack : String(err)}\n`);
  process.exit(1);
});
