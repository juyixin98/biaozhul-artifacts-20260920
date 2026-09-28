import { describe, expect, it } from 'vitest';
import { loadConfig } from '../src/config/index.js';

describe('config', () => {
  it('uses local-safe defaults', () => {
    const cfg = loadConfig({});
    expect(cfg.port).toBe(3000);
    expect(cfg.dbPath).toBe('./data/contract-diff.sqlite');
  });

  it('reads overrides from the environment', () => {
    const cfg = loadConfig({ PORT: '4010', CONTRACT_DIFF_DB: ':memory:' });
    expect(cfg.port).toBe(4010);
    expect(cfg.dbPath).toBe(':memory:');
  });

  it('rejects an invalid port', () => {
    expect(() => loadConfig({ PORT: '70000' })).toThrow(/invalid PORT/);
  });
});
