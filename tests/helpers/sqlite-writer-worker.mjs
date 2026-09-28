// Worker for cross-connection SQLite concurrency tests. Pure JavaScript so it
// runs under Node directly without a TypeScript loader; it deliberately uses
// only better-sqlite3 (the serialization primitive), not the project kernel.
//
// workerData: { dbPath, label, rows, holdMs }
// Opens its OWN connection, then in one IMMEDIATE transaction inserts `rows`
// versions, holding the write lock briefly to force a contender to wait.
import { Worker, isMainThread, parentPort, workerData } from 'node:worker_threads';
import Database from 'better-sqlite3';

if (!isMainThread) {
  const { dbPath, label, rows, holdMs } = workerData;
  try {
    const db = new Database(dbPath);
    db.pragma('busy_timeout = 10000');
    const insert = db.prepare(
      'INSERT INTO resource_versions (resource_id, version, body_json, created_ms, deleted) VALUES (?, ?, ?, ?, 0)'
    );
    const txn = db.transaction(() => {
      for (let i = 1; i <= rows; i++) {
        insert.run(`${label}-r`, i, JSON.stringify({ writer: label, i }), i);
        // Keep the lock held so the other connection genuinely contends.
        // Atomics.wait on a unit32 is a legitimate synchronous sleep in workers.
        if (holdMs > 0) sleepMs(holdMs);
      }
    });
    txn.immediate();
    db.close();
    parentPort.postMessage({ ok: true, label, rows });
  } catch (err) {
    parentPort.postMessage({ ok: false, label, error: String(err && err.message) });
  }
}

function sleepMs(ms) {
  const buf = new Int32Array(new SharedArrayBuffer(4));
  Atomics.wait(buf, 0, 0, ms);
}

export { Worker };
