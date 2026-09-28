/**
 * Append-only JSONL run logger.
 *
 * Every request (and every parser-level test run) gets a monotonically
 * attributable runId and records: run id, phase transitions, key intermediate
 * state (counters/part events), and a terminal verdict with a reason.
 *
 * The file is append-only and one JSON object per line, so a failing run can be
 * replayed from the log alone. Failures in logging must never break serving.
 */

import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export type Verdict = 'SUCCESS' | 'INPUT_ERROR' | 'STATE_CONFLICT' | 'RESOURCE_LIMIT' | 'COMPUTE_FAILURE' | 'CANCELED';

export interface RunLogEntry {
  ts: string;
  runId: string;
  phase: 'open' | 'progress' | 'verdict';
  message?: string;
  state?: Record<string, unknown>;
  verdict?: Verdict;
  reasonCode?: string;
  reason?: string;
  events?: unknown[];
  durationMs?: number;
}

let counter = 0;

export function newRunId(prefix = 'run'): string {
  counter = (counter + 1) % 1_000_000;
  const t = new Date().toISOString().replace(/[^0-9]/g, '').slice(0, 17);
  const seq = String(counter).padStart(4, '0');
  const rand = Math.floor(Math.random() * 0x10000)
    .toString(16)
    .padStart(4, '0');
  return `${prefix}-${t}-${seq}-${rand}`;
}

export class RunLogger {
  readonly path: string | null;

  constructor(path: string | null) {
    this.path = path;
    if (path) {
      mkdirSync(dirname(path), { recursive: true });
    }
  }

  record(entry: RunLogEntry): void {
    if (!this.path) return;
    try {
      appendFileSync(this.path, `${JSON.stringify(entry)}\n`);
    } catch {
      // diagnostics must never break the request path
    }
  }

  open(runId: string, state?: Record<string, unknown>): void {
    this.record({ ts: new Date().toISOString(), runId, phase: 'open', state });
  }

  progress(runId: string, message: string, state?: Record<string, unknown>): void {
    this.record({ ts: new Date().toISOString(), runId, phase: 'progress', message, state });
  }

  verdict(
    runId: string,
    verdict: Verdict,
    reason: { code?: string; message?: string },
    state?: Record<string, unknown>,
    events?: unknown[],
    durationMs?: number
  ): void {
    this.record({
      ts: new Date().toISOString(),
      runId,
      phase: 'verdict',
      verdict,
      reasonCode: reason.code,
      reason: reason.message,
      state,
      events,
      durationMs
    });
  }
}
