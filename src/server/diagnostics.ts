/**
 * Diagnostics interface.
 *
 * Every upload gets a `runId`. A {@link RunLogger} records:
 *  - the run id and start timestamp (so a failure can be replayed);
 *  - key intermediate states: envelope boundary, part begin, chunk progress,
 *    boundary kinds (intermediate/terminal), commit;
 *  - a final verdict: `committed` or `failed` with the explicit error class
 *    (INPUT_ERROR | STATE_CONFLICT | RESOURCE_LIMIT | COMPUTE_ERROR) and code.
 *
 * Records are appended to a JSONL file (one JSON object per line) and kept in
 * an in-memory ring buffer served at GET /diagnostics/runs/:id. Nothing here
 * records file CONTENT — only sizes, names and state transitions.
 */

import { appendFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { isMultipartError, type MultipartError } from '../protocol/errors.js';
import type {
  CommittedSubmission,
  PartMeta,
} from '../protocol/types.js';

export type Verdict = 'committed' | 'failed' | 'aborted';

export interface RunEvent {
  t: number; // ms since run start
  event: string;
  [key: string]: unknown;
}

export interface RunRecord {
  runId: string;
  startedAt: string;
  verdict: Verdict | 'open';
  events: RunEvent[];
  errorClass?: string;
  errorCode?: string;
  reason?: string;
  result?: CommittedSubmission;
}

const RING_CAPACITY = 256;

export class RunLogger {
  private readonly start = Date.now();
  private readonly events: RunEvent[] = [];
  private record: RunRecord;
  private settled = false;

  constructor(
    readonly runId: string = randomUUID(),
    private readonly logPath?: string,
  ) {
    this.record = {
      runId: this.runId,
      startedAt: new Date(this.start).toISOString(),
      verdict: 'open',
      events: this.events,
    };
  }

  event(name: string, fields: Record<string, unknown> = {}): void {
    this.events.push({ t: Date.now() - this.start, event: name, ...fields });
  }

  envelope(boundaryLength: number): void {
    this.event('envelope', { boundaryLength });
  }

  partBegin(meta: PartMeta, index: number): void {
    this.event('part_begin', {
      index,
      name: meta.name,
      kind: meta.kind,
      ...(meta.filename !== undefined ? { filename: meta.filename } : {}),
      contentType: meta.contentType,
    });
  }

  progress(partIndex: number, partBytes: number, totalBytes: number): void {
    this.event('progress', { partIndex, partBytes, totalBytes });
  }

  boundary(kind: 'intermediate' | 'terminal', totalBytes: number): void {
    this.event('boundary', { kind, totalBytes });
  }

  commit(result: CommittedSubmission): void {
    this.record.verdict = 'committed';
    this.record.result = result;
    this.event('commit', { submissionId: result.id });
    this.settled = true;
    void this.persist();
  }

  fail(err: unknown): void {
    if (this.settled) return;
    this.settled = true;
    const mp = isMultipartError(err) ? (err as MultipartError) : undefined;
    this.record.verdict = 'failed';
    this.record.errorClass = mp?.errorClass ?? 'COMPUTE_ERROR';
    this.record.errorCode = mp?.code ?? 'DB_ERROR';
    this.record.reason = err instanceof Error ? err.message : String(err);
    if (mp && Object.keys(mp.details).length > 0) {
      this.event('error_details', { details: mp.details });
    }
    this.event('fail', {
      errorClass: this.record.errorClass,
      errorCode: this.record.errorCode,
      reason: this.record.reason,
    });
    void this.persist();
  }

  abort(reason: string): void {
    if (this.settled) return;
    this.settled = true;
    this.record.verdict = 'aborted';
    this.record.reason = reason;
    this.event('abort', { reason });
    void this.persist();
  }

  snapshot(): RunRecord {
    // Return a shallow copy with a copy of the events array.
    return { ...this.record, events: [...this.events] };
  }

  private async persist(): Promise<void> {
    if (!this.logPath) return;
    try {
      await mkdir(path.dirname(this.logPath), { recursive: true });
      await appendFile(this.logPath, `${JSON.stringify(this.snapshot())}\n`);
    } catch {
      // Diagnostics must never break the request path.
    }
  }
}

/** Process-wide ring of recent runs, powering the diagnostics endpoint. */
export class RunRegistry {
  private readonly ring = new Map<string, RunRecord>();

  add(logger: RunLogger): void {
    const rec = logger.snapshot();
    this.ring.set(rec.runId, rec);
    if (this.ring.size > RING_CAPACITY) {
      const oldest = this.ring.keys().next().value;
      if (oldest !== undefined) this.ring.delete(oldest);
    }
  }

  get(runId: string): RunRecord | undefined {
    return this.ring.get(runId);
  }

  list(limit = 50): RunRecord[] {
    return [...this.ring.values()].reverse().slice(0, limit);
  }
}
