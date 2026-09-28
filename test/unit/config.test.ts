import { describe, expect, it } from 'vitest';
import { loadConfig } from '../../src/config.js';

describe('loadConfig', () => {
  it('缺省值', () => {
    const c = loadConfig({});
    expect(c.host).toBe('127.0.0.1');
    expect(c.port).toBe(3000);
    expect(c.maxRanges).toBe(16);
    expect(c.maxResponseBytes).toBe(64 * 1024 * 1024);
    expect(c.diagnosticsCapacity).toBe(500);
  });

  it('环境变量覆盖默认值', () => {
    const c = loadConfig({
      RANGE_HOST: '0.0.0.0',
      RANGE_PORT: '9000',
      RANGE_DB_PATH: ':memory:',
      RANGE_MAX_RANGES: '3',
      RANGE_MAX_RESPONSE_BYTES: '1024',
      RANGE_DIAGNOSTICS_CAPACITY: '10',
    });
    expect(c).toMatchObject({
      host: '0.0.0.0',
      port: 9000,
      dbPath: ':memory:',
      maxRanges: 3,
      maxResponseBytes: 1024,
      diagnosticsCapacity: 10,
    });
  });

  it('空串视为未设置', () => {
    expect(loadConfig({ RANGE_PORT: '' }).port).toBe(3000);
  });

  it('非正整数/非数字启动期报错并指出配置名', () => {
    expect(() => loadConfig({ RANGE_PORT: 'abc' })).toThrow(/RANGE_PORT/);
    expect(() => loadConfig({ RANGE_PORT: '0' })).toThrow(/RANGE_PORT/);
    expect(() => loadConfig({ RANGE_MAX_RANGES: '1.5' })).toThrow(/RANGE_MAX_RANGES/);
  });
});
