#!/usr/bin/env tsx
/**
 * 本地重放工具：
 *   tsx src/diag/replay.ts                 # 运行全部规范场景
 *   tsx src/diag/replay.ts S4-estimate-below-actual
 *   tsx src/diag/replay.ts --from-log logs/runs.jsonl   # 只读历史日志并渲染
 *
 * 输出保留：运行编号、阶段中间状态、判断理由、逐字段成本树。
 */
import { appSchema } from '../fixtures/schemaDef.js';
import { createSqliteAdapter } from '../state/sqliteAdapter.js';
import { createService } from './service.js';
import { renderCostTree } from './costTreeView.js';
import { RunStore } from './runStore.js';
import { SCENARIOS } from './scenarios.js';

function main(): void {
  const args = process.argv.slice(2);
  const fromLogIdx = args.indexOf('--from-log');
  if (fromLogIdx >= 0) {
    const path = args[fromLogIdx + 1];
    const records = RunStore.readLog(path);
    for (const rec of records) printRecord(rec);
    return;
  }

  const only = args.find((a) => !a.startsWith('--'));
  const scenarios = only ? SCENARIOS.filter((s) => s.runId === only) : SCENARIOS;
  if (scenarios.length === 0) {
    console.error(`no scenario matched ${only ?? ''}; choices: ${SCENARIOS.map((s) => s.runId).join(', ')}`);
    process.exitCode = 1;
    return;
  }

  const logPath = process.env.RUN_LOG ?? 'logs/runs.jsonl';
  const adapter = createSqliteAdapter();
  const service = createService(appSchema(), adapter, logPath);

  for (const sc of scenarios) {
    console.log('='.repeat(96));
    console.log(`RUN ${sc.runId} — ${sc.title}`);
    console.log(`预算=${sc.budget}  预期：${sc.expectation}`);
    const { runId, outcome } = service.run({
      runId: sc.runId,
      query: sc.query,
      variables: sc.variables,
      budget: sc.budget,
    });
    const rec = service.store.get(runId)!;
    console.log('-'.repeat(96));
    console.log('阶段中间状态:');
    for (const p of rec.phases) {
      if (p.phase === 'ESTIMATE') console.log(`  [ESTIMATE] estimated=${p.estimatedCost} budget=${p.budget}`);
      else if (p.phase === 'EXECUTE') console.log(`  [EXECUTE] ${p.status} actual=${p.actualCost} — ${p.detail}`);
      else console.log(`  [${p.phase}] ${p.detail}`);
    }
    if (!outcome.ok) {
      const e = outcome.error!;
      console.log(`判定：REJECTED category=${e.category} phase=${e.phase}`);
      console.log(`理由：${e.message}`);
      if (e.path) console.log(`路径：${e.path}`);
      console.log(`上下文：${JSON.stringify(e.context)}`);
      continue;
    }
    const r = outcome.result!;
    console.log(`判定：${r.status}  estimated=${r.estimatedCost} actual=${r.actualCost} budget=${r.budget} estimateBelowActual=${r.estimateBelowActual}`);
    if (r.cancelled) {
      console.log(`取消传播：path=${r.cancelled.path}${r.cancelled.fragment ? ` fragment=${r.cancelled.fragment}` : ''} spent=${r.cancelled.spent}/${r.cancelled.budget}`);
      console.log(`理由：${r.cancelled.reason}`);
    }
    console.log('-'.repeat(96));
    console.log('逐字段【实际】成本树:');
    console.log(renderCostTree(r.costTree));
    console.log('结果数据（截断展示）:');
    console.log(JSON.stringify(r.data).slice(0, 600));
  }
  adapter.close();
  console.log('='.repeat(96));
  console.log(`运行记录已追加到 ${logPath}（可用 --from-log 重放）`);
}

function printRecord(rec: ReturnType<typeof RunStore.readLog>[number]): void {
  console.log('='.repeat(96));
  console.log(`RUN ${rec.runId} @ ${rec.startedAt}`);
  console.log(`verdict: ${rec.verdict}`);
  if (rec.outcome.kind === 'RESULT') {
    const r = rec.outcome.result;
    console.log(`status=${r.status} estimated=${r.estimatedCost} actual=${r.actualCost} budget=${r.budget}`);
    console.log(renderCostTree(r.costTree));
  } else if (rec.outcome.kind === 'ERROR') {
    console.log(`error ${rec.outcome.category} @ ${rec.outcome.phase}: ${rec.outcome.message}`);
  }
}

main();
