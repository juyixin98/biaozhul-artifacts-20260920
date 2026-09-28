#!/usr/bin/env node
/**
 * Local end-to-end demo.
 *
 * Boots the REAL Fastify server as a child process on an ephemeral port with a
 * throwaway SQLite file, then drives the four required cases over HTTP:
 *   1. diamond (healthy)
 *   2. required-failure
 *   3. optional-timeout
 *   4. version-mismatch
 * and finally fetches the replayable event stream of the last run.
 *
 * No external services or accounts are involved.
 */
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PORT = 3210;
const HOST = '127.0.0.1';
const BASE = `http://${HOST}:${PORT}`;
const workdir = mkdtempSync(join(tmpdir(), 'composite-demo-'));

const server = spawn(process.execPath, ['--import', 'tsx', 'src/server.ts'], {
  env: {
    ...process.env,
    PORT: String(PORT),
    HOST,
    DB_PATH: join(workdir, 'runs.sqlite'),
    LOG_PATH: join(workdir, 'runs.jsonl'),
    DEFAULT_TIMEOUT_MS: '300',
  },
  stdio: ['ignore', 'pipe', 'pipe'],
});

server.stdout.on('data', (d) => process.stdout.write(`[server] ${d}`));
server.stderr.on('data', (d) => process.stderr.write(`[server] ${d}`));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForHealthy(deadlineMs = 15000) {
  const start = Date.now();
  for (;;) {
    try {
      const res = await fetch(`${BASE}/health`);
      if (res.ok) return;
    } catch {
      // not up yet
    }
    if (Date.now() - start > deadlineMs) throw new Error('server did not become healthy in time');
    await sleep(100);
  }
}

function heading(text) {
  console.log(`\n${'='.repeat(72)}\n${text}\n${'='.repeat(72)}`);
}

async function compose(label, body) {
  heading(label);
  const res = await fetch(`${BASE}/v1/compose`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const json = await res.json();
  const run = json.run;
  console.log(`HTTP ${res.status}   runId=${run.runId}   status=${run.status}`);
  console.log('data       :', JSON.stringify(run.data));
  console.log(
    'nodes      :',
    run.nodes
      .map((n) => `${n.callId}=${n.state}${n.attempts ? `(${n.attempts})` : ''}`)
      .join('  '),
  );
  console.log(
    'fields     :',
    run.fields.map((f) => `${f.field}[${f.requirement[0]}]=${f.state}`).join('  '),
  );
  if (run.limitations.length) {
    console.log(
      'limitations:',
      run.limitations.map((l) => `${l.type}(${l.sources.join(',')})`).join('  '),
    );
  }
  if (run.errors.length) {
    console.log(
      'errors     :',
      run.errors.map((e) => `${e.category}/${e.reason}`).join('  '),
    );
  }
  return run;
}

async function showEvents(runId, max = 999) {
  heading(`replay events for ${runId}`);
  const res = await fetch(`${BASE}/runs/${runId}/events`);
  const body = await res.json();
  for (const evt of body.events.slice(0, max)) {
    const node = evt.node ? ` ${evt.node}` : '';
    console.log(`#${String(evt.seq).padStart(2)} ${evt.type.padEnd(22)}${node}  ${evt.message}`);
    if (evt.data?.rule) console.log(`        └─ rule=${evt.data.rule}`);
  }
  console.log(`(${body.count} events total)`);
}

async function main() {
  try {
    await waitForHealthy();

    await compose('CASE 1 — diamond dependency (healthy)', {
      contract: 'orderSummary',
      input: { userId: 'u-1001', sku: 'sku-1' },
    });

    await compose('CASE 2 — required source failure (catalog fails)', {
      contract: 'orderSummary',
      input: { userId: 'u-1001', sku: 'sku-1' },
      scenario: 'required-failure',
    });

    const timed = await compose('CASE 3 — optional source timeout (promotions, 80ms deadline)', {
      contract: 'orderSummary',
      input: { userId: 'u-1001', sku: 'sku-1' },
      scenario: 'optional-timeout',
      timeoutMs: 80,
    });

    await compose('CASE 4 — source version inconsistency', {
      contract: 'orderSummary',
      input: { userId: 'u-1001', sku: 'sku-1' },
      scenario: 'version-mismatch',
    });

    await showEvents(timed.runId);
    heading('diagnostics: run index');
    const runs = await (await fetch(`${BASE}/runs?limit=5`)).json();
    for (const r of runs.runs) {
      console.log(`${r.runId}  ${r.status.padEnd(8)} ${r.contract}`);
    }
    console.log(`\nDemo artifacts (db + jsonl) are in: ${workdir}`);
  } catch (err) {
    console.error('demo failed:', err);
    process.exitCode = 1;
  } finally {
    server.kill('SIGTERM');
  }
}

main();
