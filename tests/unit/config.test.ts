import { describe, expect, it } from 'vitest';
import { loadConfig } from '../../src/config.js';

describe('loadConfig', () => {
  it('applies defaults when environment is empty', () => {
    const cfg = loadConfig({}, '/tmp/project');
    expect(cfg.port).toBe(8080);
    expect(cfg.host).toBe('127.0.0.1');
    expect(cfg.dbPath).toBe('/tmp/project/data/app.db');
    expect(cfg.logLevel).toBe('info');
  });

  it('honors valid overrides', () => {
    const cfg = loadConfig(
      { PORT: '9090', HOST: '0.0.0.0', DB_PATH: '/tmp/x.db', LOG_LEVEL: 'debug' },
      '/tmp/project'
    );
    expect(cfg.port).toBe(9090);
    expect(cfg.host).toBe('0.0.0.0');
    expect(cfg.dbPath).toBe('/tmp/x.db');
    expect(cfg.logLevel).toBe('debug');
  });

  it.each(['0', '-1', '70000', 'abc'])('fails fast on invalid PORT %j', (port) => {
    expect(() => loadConfig({ PORT: port }, '/tmp/project')).toThrow(/Invalid PORT/);
  });

  it('fails fast on an invalid LOG_LEVEL', () => {
    expect(() => loadConfig({ LOG_LEVEL: 'verbose' }, '/tmp/project')).toThrow(/Invalid LOG_LEVEL/);
  });
});
