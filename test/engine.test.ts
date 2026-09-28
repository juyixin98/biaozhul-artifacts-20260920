import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makeService } from './helpers/harness.js';
import { S4_ESTIMATED, S5_ESTIMATED } from './helpers/reference.js';

test('engine S3: 循环片段在 VALIDATE 阶段被拒，类别 STATE_CONFLICT，不产生执行成本', () => {
  // Arrange
  const { service, adapter } = makeService();
  // Act
  const { outcome } = service.run({ runId: 't-s3', budget: 100_000, query: '{ users { ...LoopA } }' });
  // Assert
  assert.equal(outcome.ok, false);
  assert.equal(outcome.error?.category, 'STATE_CONFLICT');
  assert.equal(outcome.error?.phase, 'VALIDATE');
  const rec = service.store.get('t-s3')!;
  assert.ok(!rec.phases.some((p) => p.phase === 'EXECUTE'), '拒绝前不得进入执行阶段');
  adapter.close();
});

test('engine S5: 静态预算门在执行前拒绝 RESOURCE_EXHAUSTED，phase=STATIC_GATE', () => {
  const { service, adapter } = makeService();
  const { outcome } = service.run({
    runId: 't-s5',
    budget: 100,
    query: '{ users { id name posts { id title } } }',
  });
  assert.equal(outcome.ok, false);
  assert.equal(outcome.error?.category, 'RESOURCE_EXHAUSTED');
  assert.equal(outcome.error?.phase, 'STATIC_GATE');
  assert.equal(outcome.error?.context.estimatedCost, S5_ESTIMATED); // 181
  adapter.close();
});

test('engine S4: 估计低于实际时返回 PARTIAL，estimateBelowActual=true，日志含 ESTIMATE 与取消理由', () => {
  const { service, adapter } = makeService();
  const { runId, outcome } = service.run({
    runId: 't-s4',
    budget: 1200,
    query: '{ users { id posts { id tags { name weight } } } }',
  });
  assert.equal(outcome.ok, true);
  const r = outcome.result!;
  assert.equal(r.status, 'PARTIAL');
  assert.equal(r.estimatedCost, S4_ESTIMATED); // 921
  assert.equal(r.estimateBelowActual, true);
  assert.ok(r.cancelled && r.cancelled.spent <= 1200);

  const rec = service.store.get(runId)!;
  const estimatePhase = rec.phases.find((p) => p.phase === 'ESTIMATE');
  assert.equal(estimatePhase && 'estimatedCost' in estimatePhase && estimatePhase.estimatedCost, 921);
  assert.match(rec.verdict, /^PARTIAL/);
  adapter.close();
});

test('engine: 变量类型错误为 INPUT_INVALID，且先于任何字段校验发生', () => {
  const { service, adapter } = makeService();
  const { outcome } = service.run({
    runId: 't-badvar',
    budget: 100,
    query: 'query ($uid: INT!) { userById(id: $uid) { id } }',
    variables: { uid: 'nope' },
  });
  assert.equal(outcome.ok, false);
  assert.equal(outcome.error?.category, 'INPUT_INVALID');
  assert.equal(outcome.error?.phase, 'VALIDATE');
  adapter.close();
});

test('engine: 非法 budget 本身属于 INPUT_INVALID', () => {
  const { service, adapter } = makeService();
  const { outcome } = service.run({ runId: 't-budget', budget: -1, query: '{ users { id } }' });
  assert.equal(outcome.error?.category, 'INPUT_INVALID');
  adapter.close();
});

test('engine: 完整成功路径判定串包含 estimated/actual/budget', () => {
  const { service, adapter } = makeService();
  const { runId } = service.run({ runId: 't-ok', budget: 10_000, query: '{ users { id name } }' });
  const rec = service.store.get(runId)!;
  assert.match(rec.verdict, /COMPLETE estimated=\d+ actual=\d+ within budget=10000/);
  adapter.close();
});

test('engine: 未选择的适配器错误归类 COMPUTATION_FAILED（缺失对象解析器）', async () => {
  // userById 由适配器支持；这里构造一个 schema 声明了对象字段但适配器无解析器的情形
  const { buildSchema } = await import('../src/schema/schema.js');
  const { createService } = await import('../src/diag/service.js');
  const { createSqliteAdapter } = await import('../src/state/sqliteAdapter.js');
  const schema = buildSchema({
    queryType: 'Query',
    types: [
      {
        name: 'Query',
        fields: [
          { name: 'ghost', type: 'User', list: false, declaredUpperBound: 0, multiplier: 1 },
        ],
      },
      {
        name: 'User',
        fields: [{ name: 'id', type: 'ID', list: false, declaredUpperBound: 0, multiplier: 1 }],
      },
    ],
  });
  const adapter = createSqliteAdapter();
  const svc = createService(schema, adapter);
  const { outcome } = svc.run({ runId: 't-compute', budget: 100, query: '{ ghost { id } }' });
  assert.equal(outcome.ok, false);
  assert.equal(outcome.error?.category, 'COMPUTATION_FAILED');
  adapter.close();
});
