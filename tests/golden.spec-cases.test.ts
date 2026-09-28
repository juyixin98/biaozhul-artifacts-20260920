/**
 * 验收核心：对照手写固定规范样例(fixtures/spec-cases.json)逐条核验。
 * 参考答案独立于被测实现（人工按规范推导），这里只做“实现输出 == 固定答案”。
 * 每条断言具体 data、error.path、extensions.category 与决策原因，
 * 而不是“接口能调用”。
 */

import { describe, expect, it } from 'vitest';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createHarness, type TestHarness } from './helpers/harness.js';
import type { RunOutcome } from '../src/graphql/index.js';

interface ExpectedError {
  path?: Array<string | number>;
  category: string;
  messageIncludes?: string;
}

interface SpecCase {
  id: string;
  title: string;
  query: string;
  variables: Record<string, unknown> | null;
  expectedDecision: 'accepted' | 'rejected' | 'indeterminate';
  expectedReasons?: string[];
  expectedData?: unknown;
  expectedErrors: ExpectedError[];
}

interface SpecFile {
  cases: SpecCase[];
}

const fixturesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../fixtures',
);

const specFile = JSON.parse(
  await readFile(path.join(fixturesDir, 'spec-cases.json'), 'utf8'),
) as SpecFile;

describe('golden spec cases（固定规范样例对照）', () => {
  // 每个用例独立内存库，串行执行以保证 counter 夹具确定性。
  for (const specCase of specFile.cases) {
    it(`${specCase.id}: ${specCase.title}`, async () => {
      const harness = await createHarness();
      const outcome = await harness.run({
        query: specCase.query,
        variables: specCase.variables,
      });

      assert.equal(
        outcome.decision,
        specCase.expectedDecision,
        `decision mismatch: ${JSON.stringify(outcome.result.errors)}`,
      );

      if (specCase.expectedReasons) {
        for (const reason of specCase.expectedReasons) {
          assert.ok(
            outcome.reasons.includes(reason),
            `expected reason ${reason}, got ${outcome.reasons.join(', ')}`,
          );
        }
      }

      assertData(outcome, specCase);
      assertErrors(outcome, specCase);
    });
  }
});

function assertData(outcome: RunOutcome, specCase: SpecCase): void {
  const dataPresent = Object.prototype.hasOwnProperty.call(outcome.result, 'data');
  if (specCase.expectedData === 'ABSENT') {
    assert.equal(dataPresent, false, '解析/校验拒绝时不应出现 data 字段');
    return;
  }
  assert.equal(dataPresent, true, '应返回 data 字段');
  // 深比较完整数据形状（键顺序不敏感）。
  expect(outcome.result.data).toEqual(specCase.expectedData);
}

function assertErrors(outcome: RunOutcome, specCase: SpecCase): void {
  const actual = outcome.result.errors ?? [];
  assert.equal(
    actual.length,
    specCase.expectedErrors.length,
    `错误数量不符: ${JSON.stringify(actual.map((e) => ({ msg: e.message, path: e.path, cat: e.category })), null, 2)}`,
  );

  for (const expected of specCase.expectedErrors) {
    const match = expected.path
      ? actual.find((e) => JSON.stringify(e.path ?? []) === JSON.stringify(expected.path))
      : actual[0];
    assert.ok(match, `未找到 path=${JSON.stringify(expected.path)} 的错误`);
    assert.equal(match.category, expected.category);
    if (expected.messageIncludes) {
      assert.ok(
        match.message.includes(expected.messageIncludes),
        `错误消息应包含 "${expected.messageIncludes}"，实际：${match.message}`,
      );
    }
    // 失败必须带结构化类别，而不是统一 500。
    assert.ok(
      ['PARSE', 'VALIDATION', 'COERCION', 'RESOLVER', 'INTERNAL'].includes(match.category),
      `未知错误类别: ${match.category}`,
    );
  }
}
