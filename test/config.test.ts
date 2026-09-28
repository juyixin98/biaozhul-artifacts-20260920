import { describe, expect, it } from 'vitest';
import { loadConfig } from '../src/config';

describe('loadConfig — fail-fast validation with local defaults', () => {
  it('uses local defaults for an empty environment', () => {
    const c = loadConfig({});
    expect(c.httpPort).toBe(8080);
    expect(c.httpHost).toBe('127.0.0.1');
    expect(typeof c.databasePath).toBe('string');
    expect(c.logToStdout).toBe(false);
    expect(c.prettyStorage).toBe(false);
  });

  it('parses a valid PORT and boolean flags', () => {
    const c = loadConfig({ PORT: '9099', HOST: '0.0.0.0', LOG_STDOUT: '1', PRETTY_STORAGE: '1' });
    expect(c.httpPort).toBe(9099);
    expect(c.httpHost).toBe('0.0.0.0');
    expect(c.logToStdout).toBe(true);
    expect(c.prettyStorage).toBe(true);
  });

  it('rejects an out-of-range or non-numeric PORT', () => {
    expect(() => loadConfig({ PORT: '70000' })).toThrow(/Invalid PORT/);
    expect(() => loadConfig({ PORT: 'abc' })).toThrow(/Invalid PORT/);
  });
});
