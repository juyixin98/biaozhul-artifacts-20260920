/**
 * End-to-end HTTP tests through Fastify's full content-type parser chain
 * (routing, custom parser, streaming handler, SQLite, diagnostics).
 *
 * Cancel/abort behavior over a real TCP socket lives in http-cancel.test.ts.
 */

import { mkdtempSync, readFileSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { buildApp } from '../src/app/app.js';
import type { AppConfig } from '../src/config.js';
import { RunLogger } from '../src/diagnostics/run-logger.js';
import { DEFAULT_FILE_POLICY, DEFAULT_LIMITS } from '../src/protocol/types.js';
import { SubmissionStore } from '../src/storage/submission-store.js';
import { removeDir } from './helpers.js';

const PNG_1X1 = Buffer.from(
  '89504e470d0a1a0a0000000d4948445200000001000000010806000000' +
    '1f15c4890000000d49444154789c6360000002000100ffff0300000600' +
    '0557bfabd40000000049454e44ae426082',
  'hex'
);

function multipart(boundary: string, parts: Array<{ head: string; body: Buffer }>): Buffer {
  const blocks = parts.map((p) =>
    Buffer.concat([Buffer.from('\r\n' + p.head, 'utf8'), p.body])
  );
  return Buffer.concat([
    Buffer.from(`--${boundary}`),
    ...blocks.flatMap((b, i) => (i === 0 ? [b] : [Buffer.from(`\r\n--${boundary}`), b])),
    Buffer.from(`\r\n--${boundary}--\r\n`)
  ]);
}

describe('HTTP API', () => {
  let root: string;
  let config: AppConfig;
  let store: SubmissionStore;
  let logger: RunLogger;
  let app: ReturnType<typeof buildApp>;

  beforeEach(async () => {
    root = mkdtempSync(join(tmpdir(), 'mp-http-'));
    config = {
      port: 0,
      host: '127.0.0.1',
      dataDir: root,
      tmpDir: join(root, 'tmp'),
      filesDir: join(root, 'files'),
      dbPath: join(root, 'app.sqlite'),
      runLogPath: join(root, 'run-log.jsonl'),
      limits: { ...DEFAULT_LIMITS },
      filePolicy: { ...DEFAULT_FILE_POLICY },
      requireFilePart: true
    };
    store = new SubmissionStore(config.dbPath);
    logger = new RunLogger(config.runLogPath);
    app = buildApp({ config, store, logger });
    await app.ready();
  });

  afterEach(async () => {
    await app.close();
    store.close();
    removeDir(root);
  });

  it('accepts a valid field+file upload and makes it queryable', async () => {
    const boundary = '----testB';
    const body = multipart(boundary, [
      { head: 'Content-Disposition: form-data; name="title"\r\n\r\n', body: Buffer.from('hello') },
      {
        head:
          'Content-Disposition: form-data; name="pic"; filename="pixel.png"\r\nContent-Type: image/png\r\n\r\n',
        body: PNG_1X1
      }
    ]);

    const res = await app.inject({
      method: 'POST',
      url: '/upload',
      headers: { 'content-type': `multipart/form-data; boundary=${boundary}` },
      payload: body
    });
    expect(res.statusCode, res.body).toBe(201);
    const json = res.json() as {
      runId: string;
      submission: { id: string; partsCount: number; filesCount: number; parts: Array<Record<string, unknown>> };
    };
    expect(json.submission.partsCount).toBe(2);
    expect(json.submission.filesCount).toBe(1);
    const filePart = json.submission.parts.find((p) => p.type === 'file')!;
    expect(filePart.filename).toBe('pixel.png');
    expect(filePart.size).toBe(PNG_1X1.length);

    // visible through the query API
    const got = await app.inject({ method: 'GET', url: `/submissions/${json.submission.id}` });
    expect(got.statusCode).toBe(200);
    expect((got.json() as { partsCount: number }).partsCount).toBe(2);

    // and the file downloads byte-identically
    const dl = await app.inject({ method: 'GET', url: filePart.href as string });
    expect(dl.statusCode).toBe(200);
    const downloaded = (dl as unknown as { rawPayload: Buffer }).rawPayload;
    expect(downloaded.equals(PNG_1X1)).toBe(true);
    expect(dl.headers['x-content-sha256']).toBe(filePart.sha256);

    // temp data reclaimed, permanent file present
    expect(readdirSync(config.tmpDir)).toEqual([]);
    expect(readdirSync(config.filesDir)).toHaveLength(1);
  });

  it('streams the same bytes split into many tiny writes successfully', async () => {
    const boundary = 'B';
    const body = multipart(boundary, [
      {
        head: 'Content-Disposition: form-data; name="f"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\n',
        body: Buffer.from('chunked-content')
      }
    ]);
    // inject accepts a payload stream
    const { Readable } = await import('node:stream');
    let off = 0;
    const stream = new Readable({
      read() {
        if (off >= body.length) {
          this.push(null);
          return;
        }
        const n = Math.min(3, body.length - off);
        this.push(body.subarray(off, off + n));
        off += n;
      }
    });
    const res = await app.inject({
      method: 'POST',
      url: '/upload',
      headers: { 'content-type': `multipart/form-data; boundary=${boundary}` },
      payload: stream
    });
    expect(res.statusCode, res.body).toBe(201);
  });

  it('rejects missing terminating boundary (400 INPUT_ERROR)', async () => {
    const body = Buffer.from(
      '--B\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\nabc\r\n--B'
    );
    const res = await app.inject({
      method: 'POST',
      url: '/upload',
      headers: { 'content-type': 'multipart/form-data; boundary=B' },
      payload: body
    });
    expect(res.statusCode).toBe(400);
    const json = res.json() as { error: string; errorClass: string };
    expect(json.error).toBe('MISSING_TERMINATING_BOUNDARY');
    expect(json.errorClass).toBe('INPUT_ERROR');
    // nothing visible
    expect(readdirSync(config.filesDir)).toEqual([]);
  });

  it('rejects a malicious traversal filename (400) and cleans temp data', async () => {
    const body = multipart('B', [
      {
        head:
          'Content-Disposition: form-data; name="f"; filename="../../evil.sh"\r\nContent-Type: text/plain\r\n\r\n',
        body: Buffer.from('x')
      }
    ]);
    const res = await app.inject({
      method: 'POST',
      url: '/upload',
      headers: { 'content-type': 'multipart/form-data; boundary=B' },
      payload: body
    });
    expect(res.statusCode).toBe(400);
    expect((res.json() as { error: string }).error).toBe('PATH_TRAVERSAL_FILENAME');
    expect(readdirSync(config.filesDir)).toEqual([]);
  });

  it('rejects a disallowed extension (400)', async () => {
    const body = multipart('B', [
      {
        head:
          'Content-Disposition: form-data; name="f"; filename="x.exe"\r\nContent-Type: application/x-msdownload\r\n\r\n',
        body: Buffer.from('MZ')
      }
    ]);
    const res = await app.inject({
      method: 'POST',
      url: '/upload',
      headers: { 'content-type': 'multipart/form-data; boundary=B' },
      payload: body
    });
    expect(res.statusCode).toBe(400);
    expect((res.json() as { error: string }).error).toBe('FILE_TYPE_REJECTED');
  });

  it('rejects wrong content type (415)', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/upload',
      headers: { 'content-type': 'application/json' },
      payload: Buffer.from('{}')
    });
    expect(res.statusCode).toBe(415);
  });

  it('rejects field-only submission with NO_FILE_PARTS', async () => {
    const body = multipart('B', [
      { head: 'Content-Disposition: form-data; name="a"\r\n\r\n', body: Buffer.from('1') }
    ]);
    const res = await app.inject({
      method: 'POST',
      url: '/upload',
      headers: { 'content-type': 'multipart/form-data; boundary=B' },
      payload: body
    });
    expect(res.statusCode).toBe(400);
    expect((res.json() as { error: string }).error).toBe('NO_FILE_PARTS');
  });

  it('enforces the total size limit with a 413 RESOURCE_LIMIT', async () => {
    config.limits.maxTotalSize = 10;
    config.limits.maxFileSize = 1000;
    const big = Buffer.alloc(50, 0x61);
    const body = multipart('B', [
      {
        head: 'Content-Disposition: form-data; name="f"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\n',
        body: big
      }
    ]);
    const res = await app.inject({
      method: 'POST',
      url: '/upload',
      headers: { 'content-type': 'multipart/form-data; boundary=B' },
      payload: body
    });
    expect(res.statusCode).toBe(413);
    expect((res.json() as { error: string }).error).toBe('TOTAL_SIZE_EXCEEDED');
  });

  it('exposes diagnostics: limits, health and replayable run log', async () => {
    const lim = await app.inject({ method: 'GET', url: '/diagnostics/limits' });
    expect(lim.statusCode).toBe(200);
    expect((lim.json() as { limits: { maxFileSize: number } }).limits.maxFileSize).toBe(
      DEFAULT_LIMITS.maxFileSize
    );

    const health = await app.inject({ method: 'GET', url: '/healthz' });
    expect(health.statusCode).toBe(200);
    expect((health.json() as { status: string }).status).toBe('ok');

    // one failed + one successful upload -> run log has both verdicts
    await app.inject({
      method: 'POST',
      url: '/upload',
      headers: { 'content-type': 'multipart/form-data; boundary=B' },
      payload: Buffer.from('--B\r\nx')
    });
    await app.inject({
      method: 'POST',
      url: '/upload',
      headers: { 'content-type': 'multipart/form-data; boundary=B' },
      payload: multipart('B', [
        {
          head: 'Content-Disposition: form-data; name="f"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\n',
          body: Buffer.from('ok')
        }
      ])
    });
    const runs = await app.inject({ method: 'GET', url: '/diagnostics/runs?tail=100' });
    expect(runs.statusCode).toBe(200);
    const entries = (runs.json() as { entries: Array<{ runId: string; verdict?: string; reasonCode?: string }> }).entries;
    const verdicts = entries.filter((e) => e.verdict).map((e) => e.verdict);
    expect(verdicts).toContain('INPUT_ERROR');
    expect(verdicts).toContain('SUCCESS');
    // log file itself is valid JSONL
    const raw = readFileSync(config.runLogPath, 'utf8')
      .trim()
      .split('\n')
      .map((l) => JSON.parse(l) as { runId: string });
    expect(new Set(raw.map((r) => r.runId)).size).toBeGreaterThanOrEqual(2);
  });

  it('unknown submission id is 404', async () => {
    const res = await app.inject({ method: 'GET', url: '/submissions/nope' });
    expect(res.statusCode).toBe(404);
  });
});
