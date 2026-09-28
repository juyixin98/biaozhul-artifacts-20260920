/**
 * Replay runner for hand-authored JSON fixtures.
 *
 * Usage:
 *   npx tsx scripts/replay.ts fixtures/replay/lost-response.json
 *   npx tsx scripts/replay.ts fixtures/replay            # runs every *.json
 *
 * For each fixture it starts a REAL Fastify server on an ephemeral port with
 * a fresh temporary SQLite database, issues real HTTP requests with fetch,
 * and asserts every expected status/body/header/error category. Logs carry
 * the run id, client id and step id so each verdict is traceable. Exits
 * non-zero if any assertion fails.
 */

import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { AddressInfo } from "node:net";
import { pathToFileURL } from "node:url";
import { buildApp } from "../src/http/app.js";
import { Kernel } from "../src/kernel/kernel.js";
import { SqliteResourceStore } from "../src/state/sqliteStore.js";
import type { FastifyInstance } from "fastify";

interface FixtureExpect {
  status?: number;
  body?: string;
  httpNoBody?: boolean;
  headers?: Record<string, string>;
  headerAbsent?: string[];
  errorCategory?: string;
  skip?: boolean;
}

interface FixtureStep {
  id: string;
  clientId: string;
  comment?: string;
  request: {
    method: string;
    path: string;
    headers: Record<string, string>;
    body?: string;
  };
  expect: FixtureExpect;
}

interface FixtureDecisionTrail {
  putStatusesInOrder?: number[];
  replayedFlagsInOrder?: boolean[];
}

interface Fixture {
  title: string;
  config: { etagStrength: "strong" | "weak"; runId: string };
  note?: string;
  steps: FixtureStep[];
  expectDecisionTrail?: FixtureDecisionTrail;
}

interface RunningServer {
  app: FastifyInstance;
  baseUrl: string;
  store: SqliteResourceStore;
  dbDir: string;
}

async function startServer(fixture: Fixture): Promise<RunningServer> {
  const dbDir = mkdtempSync(join(tmpdir(), "rvapi-replay-"));
  const dbPath = join(dbDir, "app.db");
  const store = new SqliteResourceStore({ path: dbPath, busyTimeoutMs: 5000 });
  const kernel = new Kernel({
    store,
    etagStrength: fixture.config.etagStrength,
    clock: () => Date.now(),
  });
  const app = await buildApp({
    kernel,
    store,
    etagStrength: fixture.config.etagStrength,
    diagnostics: false,
  });
  await app.listen({ port: 0, host: "127.0.0.1" });
  const port = (app.server.address() as AddressInfo).port;
  return { app, baseUrl: `http://127.0.0.1:${port}`, store, dbDir };
}

function log(line: string): void {
  // eslint-disable-next-line no-console
  console.log(`${new Date().toISOString()} ${line}`);
}

async function executeStep(
  server: RunningServer,
  runId: string,
  step: FixtureStep,
): Promise<string[]> {
  const failures: string[] = [];
  const reqId = step.id;
  const headers: Record<string, string> = {
    ...step.request.headers,
    "x-run-id": runId,
    "x-client-id": step.clientId,
    "x-request-id": reqId,
  };
  log(`[${runId}/${step.clientId}/${reqId}] --> ${step.request.method} ${step.request.path} headers=${JSON.stringify(headers)}`);

  const res = await fetch(new URL(step.request.path, server.baseUrl), {
    method: step.request.method,
    headers,
    body: step.request.body,
  });
  const text = await res.text();
  log(`[${runId}/${step.clientId}/${reqId}] <-- ${res.status} body=${JSON.stringify(text.slice(0, 120))} etag=${res.headers.get("etag") ?? "-"} version=${res.headers.get("x-resource-version") ?? "-"}`);

  const exp = step.expect;
  if (exp.status !== undefined && res.status !== exp.status) {
    failures.push(`status: expected ${exp.status}, got ${res.status}`);
  }
  if (exp.body !== undefined && text !== exp.body) {
    failures.push(`body: expected ${JSON.stringify(exp.body)}, got ${JSON.stringify(text)}`);
  }
  if (exp.httpNoBody && text !== "") {
    failures.push(`body: expected empty (${res.status}), got ${JSON.stringify(text)}`);
  }
  if (exp.headers) {
    for (const [name, value] of Object.entries(exp.headers)) {
      const actual = res.headers.get(name);
      if (actual !== value) {
        failures.push(`header ${name}: expected ${JSON.stringify(value)}, got ${JSON.stringify(actual)}`);
      }
    }
  }
  if (exp.headerAbsent) {
    for (const name of exp.headerAbsent) {
      if (res.headers.get(name) !== null) {
        failures.push(`header ${name}: expected absent, got ${JSON.stringify(res.headers.get(name))}`);
      }
    }
  }
  if (exp.errorCategory) {
    let parsed: { error?: { category?: unknown } } = {};
    try {
      parsed = JSON.parse(text) as { error?: { category?: unknown } };
    } catch {
      failures.push(`error category: expected ${exp.errorCategory} but body was not JSON`);
    }
    if (parsed.error?.category !== exp.errorCategory) {
      failures.push(`error category: expected ${exp.errorCategory}, got ${String(parsed.error?.category)}`);
    }
  }
  for (const f of failures) {
    log(`[${runId}/${step.clientId}/${reqId}] FAIL ${f}`);
  }
  return failures;
}

async function verifyDecisionTrail(server: RunningServer, fixture: Fixture): Promise<string[]> {
  const trail = fixture.expectDecisionTrail;
  if (!trail) return [];
  const failures: string[] = [];
  const res = await fetch(new URL(`/diagnostics/decisions?runId=${encodeURIComponent(fixture.config.runId)}`, server.baseUrl));
  const body = (await res.json()) as {
    records: Array<{ method: string; outcomeStatus: number; replayed: boolean }>;
  };
  if (trail.putStatusesInOrder) {
    const actual = body.records.filter((r) => r.method === "PUT").map((r) => r.outcomeStatus);
    if (JSON.stringify(actual) !== JSON.stringify(trail.putStatusesInOrder)) {
      failures.push(`decision trail PUT statuses: expected ${JSON.stringify(trail.putStatusesInOrder)}, got ${JSON.stringify(actual)}`);
    }
  }
  if (trail.replayedFlagsInOrder) {
    const actual = body.records.filter((r) => r.method === "PUT").map((r) => r.replayed);
    if (JSON.stringify(actual) !== JSON.stringify(trail.replayedFlagsInOrder)) {
      failures.push(`decision trail replayed flags: expected ${JSON.stringify(trail.replayedFlagsInOrder)}, got ${JSON.stringify(actual)}`);
    }
  }
  log(`[${fixture.config.runId}] decision trail has ${body.records.length} record(s)`);
  return failures;
}

async function runFixture(path: string): Promise<boolean> {
  const fixture = JSON.parse(readFileSync(path, "utf8")) as Fixture;
  const runId = fixture.config.runId;
  log(`[${runId}] === Replaying: ${fixture.title} (${path})`);
  if (fixture.note) log(`[${runId}] note: ${fixture.note}`);

  const server = await startServer(fixture);
  const failures: string[] = [];
  try {
    for (const step of fixture.steps) {
      if (step.expect.skip) continue;
      failures.push(...(await executeStep(server, runId, step)));
    }
    failures.push(...(await verifyDecisionTrail(server, fixture)));
  } finally {
    await server.app.close();
    server.store.close();
    rmSync(server.dbDir, { recursive: true, force: true });
  }

  if (failures.length === 0) {
    log(`[${runId}] PASS (${fixture.steps.length} steps)`);
    return true;
  }
  log(`[${runId}] FAIL with ${failures.length} assertion failure(s):`);
  for (const f of failures) log(`[${runId}]   - ${f}`);
  return false;
}

function resolveFixturePaths(arg: string): string[] {
  const target = resolve(arg);
  if (!statSync(target).isDirectory()) {
    return [target];
  }
  return readdirSync(target)
    .filter((name) => name.endsWith(".json"))
    .sort()
    .map((name) => join(target, name));
}

async function main(): Promise<void> {
  const arg = process.argv[2] ?? "fixtures/replay";
  const paths = resolveFixturePaths(arg);
  if (paths.length === 0) {
    log(`No fixtures found at ${arg}`);
    process.exit(2);
  }
  let allPassed = true;
  for (const path of paths) {
    const passed = await runFixture(path);
    allPassed = allPassed && passed;
  }
  log(allPassed ? `ALL FIXTURES PASSED (${paths.length})` : `FIXTURE FAILURES (${paths.length} file(s))`);
  process.exit(allPassed ? 0 : 1);
}

const invokedDirectly =
  process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href;

if (invokedDirectly) {
  main().catch((err: unknown) => {
    log(`runner error: ${err instanceof Error ? err.stack ?? err.message : String(err)}`);
    process.exit(2);
  });
}

export { runFixture, resolveFixturePaths };
