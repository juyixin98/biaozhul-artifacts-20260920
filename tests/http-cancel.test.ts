/**
 * Real-socket tests: the server actually listens on a TCP port. These cover
 * client cancellation mid-upload (socket destroyed before the terminating
 * boundary) and verify server-side effects directly:
 *  - no permanent files / SQLite rows are produced
 *  - the request-local temp directory is reclaimed
 *  - the run log records a CANCELED verdict with the run id
 */

import { existsSync, mkdtempSync, readFileSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import type { AddressInfo } from 'node:net';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { buildApp } from '../src/app/app.js';
import type { AppConfig } from '../src/config.js';
import { RunLogger } from '../src/diagnostics/run-logger.js';
import { DEFAULT_FILE_POLICY, DEFAULT_LIMITS } from '../src/protocol/types.js';
import { SubmissionStore } from '../src/storage/submission-store.js';
import { removeDir } from './helpers.js';

async function waitFor(predicate: () => boolean, tries = 50): Promise<void> {
  for (let i = 0; i < tries; i++) {
    try {
      if (predicate()) return;
    } catch {
      // predicate may probe a directory that has not been created yet
    }
    await new Promise((r) => setTimeout(r, 40));
  }
  throw new Error('condition was not reached in time');
}

/** A request-local dir that is absent or empty counts as reclaimed. */
function isDirEmptyOrAbsent(path: string): boolean {
  try {
    return readdirSync(path).length === 0;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') return true;
    throw err;
  }
}

describe('real TCP upload cancellation', () => {
  let root: string;
  let config: AppConfig;
  let store: SubmissionStore;
  let logger: RunLogger;
  let app: ReturnType<typeof buildApp>;
  let baseUrl: string;

  beforeEach(async () => {
    root = mkdtempSync(join(tmpdir(), 'mp-tcp-'));
    config = {
      port: 0,
      host: '127.0.0.1',
      dataDir: root,
      tmpDir: join(root, 'tmp'),
      filesDir: join(root, 'files'),
      dbPath: join(root, 'app.sqlite'),
      runLogPath: join(root, 'run-log.jsonl'),
      limits: { ...DEFAULT_LIMITS, maxFileSize: 10 * 1024 * 1024 },
      filePolicy: { ...DEFAULT_FILE_POLICY },
      requireFilePart: true
    };
    store = new SubmissionStore(config.dbPath);
    logger = new RunLogger(config.runLogPath);
    app = buildApp({ config, store, logger });
    await app.listen({ port: 0, host: '127.0.0.1' });
    baseUrl = `http://127.0.0.1:${(app.server.address() as AddressInfo).port}`;
  });

  afterEach(async () => {
    await app.close();
    store.close();
    removeDir(root);
  });

  it('completes a normal upload over a real socket', async () => {
    const boundary = 'TB';
    const body = Buffer.concat([
      Buffer.from(`--${boundary}\r\n`),
      Buffer.from('Content-Disposition: form-data; name="f"; filename="a.txt"\r\n'),
      Buffer.from('Content-Type: text/plain\r\n\r\n'),
      Buffer.from('socket-body'),
      Buffer.from(`\r\n--${boundary}--\r\n`)
    ]);
    const res = await fetch(`${baseUrl}/upload`, {
      method: 'POST',
      headers: { 'content-type': `multipart/form-data; boundary=${boundary}` },
      body
    });
    expect(res.status).toBe(201);
    const json = (await res.json()) as { submission: { filesCount: number } };
    expect(json.submission.filesCount).toBe(1);
    expect(store.count().submissions).toBe(1);
  });

  it('reclaims all temp resources when the client cancels mid-upload', async () => {
    const boundary = 'TB';
    const head = Buffer.concat([
      Buffer.from(`--${boundary}\r\n`),
      Buffer.from('Content-Disposition: form-data; name="f"; filename="big.txt"\r\n'),
      Buffer.from('Content-Type: text/plain\r\n\r\n')
    ]);
    const total = 4 * 1024 * 1024;
    let sent = 0;
    let firstChunk = true;
    const controller = new AbortController();
    const stream = new Readable({
      read() {
        if (firstChunk) {
          this.push(head);
          firstChunk = false;
          return;
        }
        if (sent >= total) {
          this.push(null);
          return;
        }
        const n = Math.min(64 * 1024, total - sent);
        this.push(Buffer.alloc(n, 0x61));
        sent += n;
        // Cancel after the file body has started landing in temp storage.
        if (sent >= 256 * 1024) controller.abort();
      }
    });

    await expect(
      fetch(`${baseUrl}/upload`, {
        method: 'POST',
        headers: { 'content-type': `multipart/form-data; boundary=${boundary}` },
        body: stream,
        signal: controller.signal,
        duplex: 'half'
      } as RequestInit)
    ).rejects.toThrow();

    // Server-side reclamation after the socket drop:
    await waitFor(() => isDirEmptyOrAbsent(config.filesDir));
    await waitFor(() => isDirEmptyOrAbsent(config.tmpDir));
    expect(store.count().submissions).toBe(0);

    // Run log records the cancellation with a stable code and run id.
    await waitFor(() => existsSync(config.runLogPath));
    const lines = readFileSync(config.runLogPath, 'utf8')
      .trim()
      .split('\n')
      .map((l) => JSON.parse(l) as { runId: string; verdict?: string; reasonCode?: string });
    const canceled = lines.filter((l) => l.verdict === 'CANCELED');
    expect(canceled.length).toBe(1);
    expect(canceled[0]!.reasonCode).toBe('UPLOAD_CANCELED');
    expect(canceled[0]!.runId).toMatch(/^up-/);
  });
});
