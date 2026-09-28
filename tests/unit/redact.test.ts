/**
 * 脱敏工具单元测试。
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { isSensitiveKey, redactValue, shapeOfVariables } from '../../src/diagnostics/redact.js';

test('isSensitiveKey: 命中常见敏感键（含前后缀变体）', () => {
  for (const key of ['email', 'userEmail', 'emailAddress', 'password', 'apiKey', 'api_key', 'token', 'x-token', 'secret', 'credentials']) {
    assert.equal(isSensitiveKey(key), true, `${key} should be sensitive`);
  }
  assert.equal(isSensitiveKey('displayName'), false);
  assert.equal(isSensitiveKey('handle'), false);
});

test('redactValue: 深层对象与数组中的敏感值被掩码', () => {
  const input = {
    user: {
      handle: 'ada',
      email: 'ada@example.test',
      profile: { backupEmail: 'bak@example.test', age: 30 },
    },

    items: [{ token: 'abc', name: 'ok' }],
  };
  const redacted = redactValue(input) as any;
  assert.equal(redacted.user.handle, 'ada');
  assert.equal(redacted.user.email, '***REDACTED***');
  assert.equal(redacted.user.profile.backupEmail, '***REDACTED***');
  assert.equal(redacted.user.profile.age, 30);
  assert.equal(redacted.items[0].token, '***REDACTED***');
  assert.equal(redacted.items[0].name, 'ok');
});

test('redactValue: 循环引用不导致栈溢出', () => {
  const a: Record<string, unknown> = { email: 'x@y.test' };
  const b: Record<string, unknown> = { a };
  b.self = b;
  a.b = b;
  const redacted = redactValue(a) as any;
  assert.equal(redacted.email, '***REDACTED***');
  assert.equal(redacted.b.self, '[Circular]');
});

test('shapeOfVariables: 仅输出形态，不输出值', () => {
  const shape = shapeOfVariables({
    id: 'u-01',
    count: 3,
    enabled: true,
    nothing: null,
    ids: ['a', 'b'],
  });
  assert.deepEqual(shape, {
    id: 'string',
    count: 'number',
    enabled: 'boolean',
    nothing: 'null',
    ids: 'list[2]',
  });
});
