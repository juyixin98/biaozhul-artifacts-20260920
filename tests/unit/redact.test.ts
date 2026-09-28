/**
 * 脱敏工具单测。
 */

import { describe, expect, it } from 'vitest';
import { redactText, redactVariables } from '../../src/diagnostics/redact.js';

describe('redact', () => {
  it('邮箱局部打码，保留首字符与域名', () => {
    expect(redactText('contact alice@example.com now')).toBe(
      'contact a***@example.com now',
    );
  });

  it('敏感键名变量值打码', () => {
    const shape = redactVariables({
      password: 'hunter2',
      token: 'abc',
      apiKey: 'k-1',
      id: 'u1',
      limit: 3,
      tags: ['a', 'b'],
    });
    expect(shape.password).toBe('<redacted>');
    expect(shape.token).toBe('<redacted>');
    expect(shape.apiKey).toBe('<redacted>');
    expect(shape.id).toBe('string');
    expect(shape.limit).toBe('number');
    expect(shape.tags).toBe('array[2]');
  });

  it('email 键同样打码（防止变量携带邮箱泄露）', () => {
    expect(redactVariables({ email: 'a@b.com' }).email).toBe('<redacted>');
  });
});
