/**
 * 编排管线 runGraphQL 的分支覆盖：
 *  - 无 operation / 多 operation 歧义 / operationName 未知 => rejected|indeterminate
 *  - 字段级错误仍属 accepted（执行已发生）
 */

import { describe, expect, it } from 'vitest';
import { runGraphQL } from '../../src/graphql/run.js';
import { createHarness } from '../helpers/harness.js';

describe('runGraphQL 编排', () => {
  it('只含片段的文档 => rejected / NO_OPERATION', async () => {
    const harness = await createHarness();
    const outcome = await runGraphQL({
      schema: harness.schema,
      query: 'fragment F on User { id }',
      context: { requestId: 'r', startedAt: 0, db: harness.db },
    });
    expect(outcome.decision).toBe('rejected');
    expect(outcome.reasons).toContain('NO_OPERATION');
    expect(outcome.result.data).toBeUndefined();
  });

  it('多操作且不指定 operationName => indeterminate / OPERATION_AMBIGUOUS', async () => {
    const harness = await createHarness();
    const outcome = await runGraphQL({
      schema: harness.schema,
      query: 'query A { users(limit: 1) { id } } query B { posts { id } }',
      context: { requestId: 'r', startedAt: 0, db: harness.db },
    });
    expect(outcome.decision).toBe('indeterminate');
    expect(outcome.reasons).toContain('OPERATION_AMBIGUOUS');
  });

  it('指定未知 operationName => rejected / OPERATION_UNKNOWN', async () => {
    const harness = await createHarness();
    const outcome = await runGraphQL({
      schema: harness.schema,
      query: 'query A { users(limit: 1) { id } }',
      operationName: 'ZZZ',
      context: { requestId: 'r', startedAt: 0, db: harness.db },
    });
    expect(outcome.decision).toBe('rejected');
    expect(outcome.reasons).toContain('OPERATION_UNKNOWN');
  });

  it('多操作文档指定正确 operationName => 执行该操作', async () => {
    const harness = await createHarness();
    const outcome = await runGraphQL({
      schema: harness.schema,
      query: 'query A { users(limit: 1) { id } } query B { posts { id } }',
      operationName: 'B',
      context: { requestId: 'r', startedAt: 0, db: harness.db },
    });
    expect(outcome.decision).toBe('accepted');
    expect(outcome.result.data).toEqual({
      posts: [{ id: 'p1' }, { id: 'p2' }, { id: 'p3' }, { id: 'p4' }],
    });
  });

  it('字段错误时 decision=accepted 且 resolverErrorCount>0', async () => {
    const harness = await createHarness();
    const outcome = await runGraphQL({
      schema: harness.schema,
      query: '{ looseTags }',
      context: { requestId: 'r', startedAt: 0, db: harness.db },
    });
    expect(outcome.decision).toBe('accepted');
    expect(outcome.reasons).toContain('EXECUTED_WITH_FIELD_ERRORS');
    expect(outcome.resolverErrorCount).toBe(1);
  });
});
