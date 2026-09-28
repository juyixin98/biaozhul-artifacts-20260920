import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { appSchema } from '../src/fixtures/schemaDef.js';
import { createSqliteAdapter } from '../src/state/sqliteAdapter.js';
import { createService } from '../src/diag/service.js';
import { RunStore } from '../src/diag/runStore.js';

test('diag: 运行日志落盘并可按运行编号重放，保留阶段中间状态与判定理由', () => {
  // Arrange
  const dir = mkdtempSync(join(tmpdir(), 'budget-gw-'));
  const logPath = join(dir, 'runs.jsonl');
  const adapter = createSqliteAdapter();
  const service = createService(appSchema(), adapter, logPath);

  // Act
  service.run({ runId: 'log-1', budget: 1200, query: '{ users { id posts { id tags { name weight } } } }' });
  service.run({ runId: 'log-2', budget: 1, query: '{ users { id } }' });
  service.run({ runId: 'log-3', budget: 100, query: '{ users { ...LoopA } }' });

  // Assert —— 文件存在
  assert.ok(existsSync(logPath));

  // 从磁盘重新读取（不经过内存 store）
  const records = RunStore.readLog(logPath);
  assert.equal(records.length, 3);

  const partial = records.find((r) => r.runId === 'log-1')!;
  assert.equal(partial.outcome.kind, 'RESULT');
  if (partial.outcome.kind !== 'RESULT') throw new Error('bad outcome');
  assert.equal(partial.outcome.result.status, 'PARTIAL');
  assert.match(partial.verdict, /^PARTIAL/);

  // 关键中间状态：ESTIMATE 带估计成本，EXECUTE 带实际成本
  const phases = partial.phases.map((p) => p.phase);
  assert.deepEqual(phases, ['PARSE', 'VALIDATE', 'ESTIMATE', 'STATIC_GATE', 'EXECUTE']);
  const estimate = partial.phases.find((p) => p.phase === 'ESTIMATE');
  assert.equal(estimate && 'estimatedCost' in estimate && estimate.estimatedCost, 921);

  // 失败类别可区分：log-2 为 RESULT/PARTIAL（运行时取消），log-3 为 STATE_CONFLICT
  const conflict = records.find((r) => r.runId === 'log-3')!;
  assert.equal(conflict.outcome.kind, 'ERROR');
  if (conflict.outcome.kind !== 'ERROR') throw new Error('bad outcome');
  assert.equal(conflict.outcome.category, 'STATE_CONFLICT');
  assert.match(conflict.verdict, /^REJECTED\[STATE_CONFLICT\]/);

  adapter.close();
});
