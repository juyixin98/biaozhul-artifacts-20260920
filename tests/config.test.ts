import { describe, expect, it } from 'vitest';
import { loadConfig } from '../src/config.js';

describe('config: fail-fast boundary validation at startup', () => {
  it('uses defaults when the environment is empty', () => {
    const cfg = loadConfig({});
    expect(cfg.host).toBe('127.0.0.1');
    expect(cfg.port).toBe(8545);
    expect(cfg.dbPath).toBe('./data/rpc.sqlite');
    expect(cfg.maxTaskDelayMs).toBe(5000);
    expect(cfg.logLevel).toBe('info');
  });

  it('accepts valid overrides', () => {
    const cfg = loadConfig({ HOST: '0.0.0.0', PORT: '9000', DB_PATH: ':memory:', TASK_MAX_DELAY_MS: '10', LOG_LEVEL: 'debug' });
    expect(cfg.port).toBe(9000);
    expect(cfg.maxTaskDelayMs).toBe(10);
    expect(cfg.logLevel).toBe('debug');
  });

  it.each([
    ['abc', 'non-numeric'],
    ['0', 'below range'],
    ['70000', 'above range'],
    ['12.5', 'non-integer'],
  ])('rejects PORT=%s (%s)', (port) => {
    expect(() => loadConfig({ PORT: port })).toThrow(/PORT/);
  });

  it.each(['-1', '1.5', 'abc'])('rejects TASK_MAX_DELAY_MS=%s', (value) => {
    expect(() => loadConfig({ TASK_MAX_DELAY_MS: value })).toThrow(/TASK_MAX_DELAY_MS/);
  });

  it('rejects an unknown log level', () => {
    expect(() => loadConfig({ LOG_LEVEL: 'verbose' })).toThrow(/LOG_LEVEL/);
  });

  it('accepts the silent log level', () => {
    expect(loadConfig({ LOG_LEVEL: 'silent' }).logLevel).toBe('silent');
  });
});
