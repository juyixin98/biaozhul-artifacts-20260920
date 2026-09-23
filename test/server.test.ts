import { afterAll, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { buildServer } from '../src/server.js';

const samplesDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'samples');
const load = (rel: string) => JSON.parse(readFileSync(join(samplesDir, rel), 'utf8'));

const app = buildServer();
afterAll(async () => {
  await app.close();
});

describe('HTTP service', () => {
  it('GET /health', async () => {
    const res = await app.inject({ method: 'GET', url: '/health' });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ status: 'ok' });
  });

  it('POST /compare with a compatible pair returns 200', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/compare',
      payload: { old: load('v1/Token.storage.json'), new: load('v2-append/Token.storage.json') },
    });
    expect(res.statusCode).toBe(200);
    expect(res.json().verdict).toBe('compatible');
  });

  it('POST /compare with an incompatible pair returns 422 and findings', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/compare',
      payload: { old: load('v1/Token.storage.json'), new: load('v2-breaking/Token.storage.json') },
    });
    expect(res.statusCode).toBe(422);
    const body = res.json();
    expect(body.verdict).toBe('incompatible');
    expect(body.stats.errors).toBeGreaterThan(0);
    expect(body.findings[0]).toHaveProperty('path');
    expect(body.findings[0]).toHaveProperty('evidence');
  });

  it('POST /compare with a malformed body returns 400', async () => {
    const res = await app.inject({ method: 'POST', url: '/compare', payload: { old: {} } });
    expect(res.statusCode).toBe(400);
    expect(res.json().error).toBe('bad_request');
  });
});
