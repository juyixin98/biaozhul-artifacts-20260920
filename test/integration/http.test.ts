import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { buildApp } from '../../src/server/app.js';
import { SubmissionRepository } from '../../src/storage/repository.js';
import { createDefaultConfig } from '../../src/protocol/config.js';
import type { FastifyInstance } from 'fastify';
import {
  encodeMultipart,
  deterministicBytes,
  type EncodedPart,
} from '../helpers/encode.js';

const root = await mkdtemp(path.join(tmpdir(), 'mp-http-'));
const BOUNDARY = '----testBoundary7';
let app: FastifyInstance;
let baseUrl: string;
let repo: SubmissionRepository;

before(async () => {
  const config = createDefaultConfig({
    tempDir: path.join(root, 'tmp'),
    limits: {
      maxPartBytes: 4096,
      maxFieldBytes: 1024,
      maxTotalBytes: 16384,
      maxHeaderBytes: 4096,
      maxParts: 8,
      maxBoundaryLength: 70,
    },
  });
  repo = new SubmissionRepository(path.join(root, 'app.db'));
  app = await buildApp({ config, repo, logPath: path.join(root, 'runs.jsonl') });
  await app.listen({ host: '127.0.0.1', port: 0 });
  const address = app.server.address();
  if (address === null || typeof address === 'string') throw new Error('no port');
  baseUrl = `http://127.0.0.1:${address.port}`;
});

after(async () => {
  await app.close();
  repo.close();
  await rm(root, { recursive: true, force: true });
});

function wire(parts: EncodedPart[], opts: Parameters<typeof encodeMultipart>[2] = {}): Buffer {
  return encodeMultipart(BOUNDARY, parts, opts);
}

async function postBuffer(
  body: Buffer,
  init?: { headers?: Record<string, string> },
): Promise<Response> {
  return fetch(`${baseUrl}/uploads`, {
    method: 'POST',
    headers: {
      'content-type': `multipart/form-data; boundary=${BOUNDARY}`,
      ...init?.headers,
    },
    body,
  });
}

async function getJson<T>(url: string): Promise<T> {
  return (await (await fetch(url)).json()) as T;
}

test('health endpoint responds ok', async () => {
  const res = await fetch(`${baseUrl}/health`);
  assert.equal(res.status, 200);
  const health = (await res.json()) as { data: { status: string } };
  assert.deepEqual(health.data, { status: 'ok' });
});

test('valid upload commits, is visible, and file bytes round-trip', async () => {
  const body = wire([
    { name: 'title', body: Buffer.from('hello') },
    {
      name: 'file',
      filename: 'note.txt',
      contentType: 'text/plain',
      body: Buffer.from('the file contents'),
    },
  ]);

  const res = await postBuffer(body);
  const raw = await res.text();
  assert.equal(res.status, 201, raw);
  const json = JSON.parse(raw) as {
    success: boolean;
    data: { runId: string; submission: { id: number; fileCount: number; fieldCount: number } };
  };
  assert.equal(json.success, true);
  assert.equal(json.data.submission.fieldCount, 1);
  assert.equal(json.data.submission.fileCount, 1);
  const submissionId = json.data.submission.id;

  // Visibility: listed and fetchable.
  interface PartView {
    id: number;
    name: string;
    value?: string;
    filename?: string;
  }
  const detail = await getJson<{ data: { submission: { parts: PartView[] } } }>(
    `${baseUrl}/submissions/${submissionId}`,
  );
  assert.equal(detail.data.submission.parts[0]!.value, 'hello');
  assert.equal(detail.data.submission.parts[1]!.filename, 'note.txt');

  const partId = detail.data.submission.parts[1]!.id;
  const blob = await fetch(`${baseUrl}/submissions/${submissionId}/parts/${partId}/blob`);
  assert.equal(blob.status, 200);
  assert.equal(await blob.text(), 'the file contents');

  // Diagnostics record for the run exists and ends committed.
  const run = await getJson<{
    data: { run: { verdict: string; events: Array<{ event: string; kind?: string }> } };
  }>(`${baseUrl}/diagnostics/runs/${json.data.runId}`);
  assert.equal(run.data.run.verdict, 'committed');
  assert.ok(run.data.run.events.some((e) => e.event === 'boundary' && e.kind === 'terminal'));
});

test('non-multipart content type is 400 INPUT_ERROR/NOT_MULTIPART', async () => {
  const res = await fetch(`${baseUrl}/uploads`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: '{}',
  });
  assert.equal(res.status, 400);
  const json = (await res.json()) as {
    error: { errorClass: string; code: string; runId?: string };
  };
  assert.equal(json.error.errorClass, 'INPUT_ERROR');
  assert.equal(json.error.code, 'NOT_MULTIPART');
  assert.ok(typeof json.error.runId === 'string');
});

test('missing closing delimiter is 400 MISSING_TERMINATOR and nothing is stored', async () => {
  const before = repo.count();
  const body = wire(
    [{ name: 'a', body: Buffer.from('x') }],
    { terminator: false },
  );
  const res = await postBuffer(body);
  assert.equal(res.status, 400);
  const json = (await res.json()) as { error: { code: string } };
  assert.equal(json.error.code, 'MISSING_TERMINATOR');
  assert.equal(repo.count(), before);
});

test('malicious filename is 400 FILENAME_REJECTED and nothing is stored', async () => {
  const before = repo.count();
  const body = wire([
    { name: 'f', filename: '../../etc/passwd', contentType: 'text/plain', body: Buffer.from('x') },
  ]);
  const res = await postBuffer(body);
  assert.equal(res.status, 400);
  const json = (await res.json()) as { error: { code: string; details: { reason: string } } };
  assert.equal(json.error.code, 'FILENAME_REJECTED');
  assert.equal(json.error.details.reason, 'separator');
  assert.equal(repo.count(), before);
});

test('oversized part is 413 RESOURCE_LIMIT/PART_TOO_LARGE', async () => {
  const body = wire([
    {
      name: 'big',
      filename: 'big.txt',
      contentType: 'text/plain',
      body: deterministicBytes(1, 8192),
    },
  ]);
  const res = await postBuffer(body);
  assert.equal(res.status, 413);
  const json = (await res.json()) as { error: { errorClass: string; code: string } };
  assert.equal(json.error.errorClass, 'RESOURCE_LIMIT');
  assert.equal(json.error.code, 'PART_TOO_LARGE');
});

test('oversized aggregate is 413 TOTAL_TOO_LARGE', async () => {
  // Five file parts, each under the 4096 per-part quota but jointly over the
  // 16384 aggregate quota, and within the 8-part cap.
  const parts: EncodedPart[] = Array.from({ length: 5 }, (_, i) => ({
    name: `f${i}`,
    filename: `f${i}.png`,
    contentType: 'image/png' as const,
    body: deterministicBytes(100 + i, 3500),
  }));
  const res = await postBuffer(wire(parts));
  assert.equal(res.status, 413);
  const json = (await res.json()) as { error: { code: string } };
  assert.equal(json.error.code, 'TOTAL_TOO_LARGE');
});

test('uploaded bytes arrive fragmented and still parse correctly', async () => {
  const body = wire([
    {
      name: 'f',
      filename: 'frag.png',
      contentType: 'image/png',
      body: deterministicBytes(77, 500),
    },
  ]);
  const res = await new Promise<http.IncomingMessage>((resolve, reject) => {
    const req = http.request(
      {
        host: '127.0.0.1',
        port: Number(new URL(baseUrl).port),
        path: '/uploads',
        method: 'POST',
        headers: {
          'content-type': `multipart/form-data; boundary=${BOUNDARY}`,
          'content-length': body.length,
        },
      },
      resolve,
    );
    req.on('error', reject);
    // Send 11-byte fragments with small delays to force chunk boundaries.
    let pos = 0;
    const push = (): void => {
      if (pos >= body.length) {
        req.end();
        return;
      }
      const next = Math.min(body.length, pos + 11);
      req.write(body.subarray(pos, next));
      pos = next;
      setTimeout(push, 2);
    };
    push();
  });
  assert.equal(res.statusCode, 201);
  const chunks: Buffer[] = [];
  for await (const c of res) chunks.push(c as Buffer);
  const json = JSON.parse(Buffer.concat(chunks).toString()) as {
    data: { submission: { totalSize: number } };
  };
  assert.equal(json.data.submission.totalSize, 500);
});

test('client cancelling mid-upload leaves no temp artifacts and no row', async () => {
  const before = repo.count();
  const body = wire([
    {
      name: 'big',
      filename: 'cancel.txt',
      contentType: 'text/plain',
      body: deterministicBytes(9, 12000),
    },
  ]);

  await new Promise<void>((resolve, reject) => {
    const req = http.request(
      {
        host: '127.0.0.1',
        port: Number(new URL(baseUrl).port),
        path: '/uploads',
        method: 'POST',
        headers: {
          'content-type': `multipart/form-data; boundary=${BOUNDARY}`,
          'content-length': body.length,
        },
      },
      (res) => {
        res.resume();
        res.on('end', () => resolve());
      },
    );
    req.on('error', () => resolve()); // socket reset expected
    req.write(body.subarray(0, 200));
    setTimeout(() => {
      req.destroy();
      resolve();
    }, 100);
  });

  // Give the server a moment to run its finally block.
  await new Promise((r) => setTimeout(r, 150));

  const leftovers = await readdir(path.join(root, 'tmp')).catch(() => []);
  assert.deepEqual(leftovers, [], 'no per-request temp directories may remain');
  assert.equal(repo.count(), before, 'a cancelled upload must never be committed');
});

test('diagnostics listing records failed runs with their error class', async () => {
  const listing = (await (await fetch(`${baseUrl}/diagnostics/runs`)).json()) as {
    data: { runs: Array<{ verdict: string; errorClass?: string; errorCode?: string }> };
  };
  assert.ok(listing.data.runs.length > 0);
  const failed = listing.data.runs.find((r) => r.verdict === 'failed');
  assert.ok(failed, 'at least one failed run should be recorded');
  assert.ok(['INPUT_ERROR', 'RESOURCE_LIMIT', 'STATE_CONFLICT', 'COMPUTE_ERROR'].includes(failed!.errorClass!));
  assert.ok(typeof failed!.errorCode === 'string');
});

test('unknown submission id returns 404', async () => {
  const res = await fetch(`${baseUrl}/submissions/99999999`);
  assert.equal(res.status, 404);
});
