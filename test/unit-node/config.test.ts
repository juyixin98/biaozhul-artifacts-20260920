import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadConfig } from '../../src/config/index.js';

test('loadConfig returns built-in defaults with an empty environment', () => {
  const c = loadConfig({});
  assert.equal(c.host, '127.0.0.1');
  assert.equal(c.port, 3000);
  assert.equal(c.databasePath, 'data/objects.db');
  assert.equal(c.limits.maxSpecs, 50);
  assert.equal(c.limits.maxResponseBytes, 16 * 1024 * 1024);
  assert.equal(c.limits.mergeGap, 1);
  assert.equal(c.logBodies, false);
});

test('loadConfig honours every documented environment variable', () => {
  const c = loadConfig({
    HOST: '0.0.0.0',
    PORT: '8080',
    DB_PATH: '/tmp/x.db',
    DIAGNOSTICS_LOG: '/tmp/x.jsonl',
    LOG_BODIES: '1',
    RANGE_MAX_SPECS: '3',
    RANGE_MAX_RESPONSE_BYTES: '1024',
    RANGE_MERGE_GAP: '0',
  });
  assert.equal(c.host, '0.0.0.0');
  assert.equal(c.port, 8080);
  assert.equal(c.databasePath, '/tmp/x.db');
  assert.equal(c.logPath, '/tmp/x.jsonl');
  assert.equal(c.logBodies, true);
  assert.deepEqual(c.limits, {
    maxSpecs: 3,
    maxResponseBytes: 1024,
    mergeGap: 0,
  });
});

test('DIAGNOSTICS_LOG empty string disables file logging but keeps stdout', () => {
  const c = loadConfig({ DIAGNOSTICS_LOG: '' });
  assert.equal(c.logPath, null);
});

test('a non-integer port fails fast with the variable name', () => {
  assert.throws(() => loadConfig({ PORT: 'soon' }), /Invalid PORT/);
});

test('a negative limit fails fast', () => {
  assert.throws(() => loadConfig({ RANGE_MAX_SPECS: '-1' }), /Invalid RANGE_MAX_SPECS/);
});

test('a non-safe-integer limit fails fast rather than silently truncating', () => {
  assert.throws(
    () => loadConfig({ RANGE_MAX_RESPONSE_BYTES: '9007199254740993' }),
    /Invalid RANGE_MAX_RESPONSE_BYTES/,
  );
});

test('RANGE_MAX_SPECS below one is rejected', () => {
  assert.throws(() => loadConfig({ RANGE_MAX_SPECS: '0' }), /RANGE_MAX_SPECS must be >= 1/);
});
