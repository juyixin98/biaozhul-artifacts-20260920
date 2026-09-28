/**
 * Real cross-connection concurrency at the SQLite layer.
 *
 * Two worker threads open INDEPENDENT better-sqlite3 connections to the same
 * WAL database and both attempt IMMEDIATE write transactions that overlap in
 * time (each holds the write lock). We assert:
 *   1. neither connection gets SQLITE_BUSY (busy_timeout lets it wait);
 *   2. all rows from both writers commit — no update is lost;
 *   3. the per-resifier primary key is intact (version sequence per resource).
 */
import { describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { Worker } from 'node:worker_threads';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import { SqliteResourceStore } from '../../src/state/sqlite-store.js';

const workerPath = fileURLToPath(new URL('../helpers/sqlite-writer-worker.mjs', import.meta.url));

function runWorker(workerData: Record<string, unknown>): Promise<{ ok: boolean; label: string; error?: string; rows?: number }> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(workerPath, { workerData });
    worker.once('message', resolve);
    worker.once('error', reject);
  });
}

describe('SQLite cross-connection write serialization', () => {
  it('overlapping IMMEDIATE transactions from two connections both commit, no SQLITE_BUSY', async () => {
    const dir = mkdtempSync(path.join(os.tmpdir(), 'rv-conc-'));
    const dbPath = path.join(dir, 'conc.db');
    // Create schema first, then workers share the file.
    const bootstrap = new SqliteResourceStore(dbPath);
    bootstrap.close();

    const started = Date.now();
    const [a, b] = await Promise.all([
      runWorker({ dbPath, label: 'A', rows: 3, holdMs: 120 }),
      // Stagger B minimally so both transactions genuinely overlap.
      new Promise<{ ok: boolean; label: string; error?: string; rows?: number }>((resolve) =>
        setTimeout(() => resolve(runWorker({ dbPath, label: 'B', rows: 3, holdMs: 120 })), 30)
      ).then((p) => p)
    ]);
    const elapsed = Date.now() - started;

    expect(a.ok, `writer A failed: ${a.error ?? ''}`).toBe(true);
    expect(b.ok, `writer B failed: ${b.error ?? ''}`).toBe(true);
    // Overlap forced waiting; serialization means wall time exceeds one hold.
    expect(elapsed).toBeGreaterThan(150);

    const verify = new SqliteResourceStore(dbPath);
    try {
      const aVersions = verify.listVersions('A-r').map((v) => v.version);
      const bVersions = verify.listVersions('B-r').map((v) => v.version);
      expect(aVersions).toEqual([1, 2, 3]);
      expect(bVersions).toEqual([1, 2, 3]);
      expect(verify.countResources()).toBe(2);
    } finally {
      verify.close();
      rmSync(dir, { recursive: true, force: true });
    }
  }, 30_000);
});
