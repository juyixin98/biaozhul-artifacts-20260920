/**
 * Structured test logging.
 *
 * Every line carries the run id, client id and request id so logs can be
 * correlated back to the exact inputs and actor of a scenario. Lines are
 * emitted when LOG_TESTS=1 and are always appended to
 * .test-output/<runId>.log so a replay can be inspected afterwards.
 */

import { appendFileSync, mkdirSync } from "node:fs";

export interface TestLogContext {
  readonly runId: string;
  readonly clientId: string;
  readonly requestId: string;
}

const enabled = process.env.LOG_TESTS === "1" || process.env.LOG_TESTS === "true";
const outDir = new URL("../../.test-output/", import.meta.url);
mkdirSync(outDir, { recursive: true });

function write(line: string): void {
  const formatted = `${new Date().toISOString()} ${line}`;
  if (enabled) {
    // eslint-disable-next-line no-console
    console.log(formatted);
  }
  appendFileSync(new URL("./all.log", outDir), formatted + "\n");
}

export function scenarioHeader(runId: string, title: string, extra?: Record<string, unknown>): void {
  write(`[${runId}] === SCENARIO: ${title}${extra ? " " + JSON.stringify(extra) : ""}`);
}

export function logRequest(
  ctx: TestLogContext,
  details: {
    method: string;
    resourceId: string;
    headers?: Record<string, string | undefined>;
    bodyPreview?: string;
  },
): void {
  write(
    `[${ctx.runId}/${ctx.clientId}/${ctx.requestId}] --> ${details.method} /resources/${details.resourceId} ` +
      `headers=${JSON.stringify(compactHeaders(details.headers ?? {}))} body=${details.bodyPreview ?? "<none>"}`,
  );
}

export function logObservation(
  ctx: TestLogContext,
  details: { version: number | null; etag: string | null; updatedAtMs: number | null },
): void {
  write(
    `[${ctx.runId}/${ctx.clientId}/${ctx.requestId}]     observed current: version=${details.version ?? "<absent>"} etag=${details.etag ?? "<absent>"} updatedAt=${details.updatedAtMs ?? "<absent>"}`,
  );
}

export function logSteps(ctx: TestLogContext, steps: ReadonlyArray<{ stage: string; result: string } & Record<string, unknown>>): void {
  for (const step of steps) {
    write(`[${ctx.runId}/${ctx.clientId}/${ctx.requestId}]     step ${step.stage} => ${step.result} detail=${JSON.stringify(step)}`);
  }
}

export function logOutcome(
  ctx: TestLogContext,
  details: {
    status: number;
    verdict: string;
    category?: string | null;
    resultingVersion?: number | null;
    resultingEtag?: string | null;
    replayed?: boolean;
    note?: string;
  },
): void {
  write(
    `[${ctx.runId}/${ctx.clientId}/${ctx.requestId}] <-- status=${details.status} verdict=${details.verdict}` +
      ` category=${details.category ?? "-"} newVersion=${details.resultingVersion ?? "-"} newEtag=${details.resultingEtag ?? "-"}` +
      ` replayed=${details.replayed ? "true" : "false"}${details.note ? " note=" + details.note : ""}`,
  );
}

export function logAssertion(runId: string, passed: boolean, message: string): void {
  write(`[${runId}] ${passed ? "PASS" : "FAIL"} assert: ${message}`);
}

function compactHeaders(headers: Record<string, string | undefined>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(headers)) {
    if (v !== undefined) out[k] = v;
  }
  return out;
}
