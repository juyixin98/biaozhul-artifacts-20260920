/**
 * Structured run log.
 *
 * Every composite run produces an ordered event stream with:
 *  - a stable `runId` (correlation id used in HTTP responses, DB rows, JSONL),
 *  - key intermediate states (scheduled/started/resolved/failed/aborted),
 *  - explicit `decision` events recording *why* the engine chose an outcome
 *    (required failure, optional degrade, dependency skip, deadline, …).
 *
 * The same events are (a) kept in memory for the diagnostics API, (b) persisted
 * to SQLite by the RunStore sink, and (c) appended to a replayable JSONL file.
 */
import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export interface LogEvent {
  seq: number;
  ts: number;
  runId: string;
  type: string;
  node?: string;
  message: string;
  data?: Record<string, unknown>;
}

export type EventSink = (event: LogEvent) => void;

export class RunLog {
  readonly events: LogEvent[] = [];
  private seq = 0;

  constructor(
    readonly runId: string,
    private readonly sinks: EventSink[] = [],
  ) {}

  event(type: string, message: string, node?: string, data?: Record<string, unknown>): LogEvent {
    const evt: LogEvent = {
      seq: this.seq++,
      ts: Date.now(),
      runId: this.runId,
      type,
      ...(node !== undefined ? { node } : {}),
      message,
      ...(data !== undefined ? { data } : {}),
    };
    this.events.push(evt);
    for (const sink of this.sinks) {
      try {
        sink(evt);
      } catch {
        // A broken log sink must never alter execution semantics.
      }
    }
    return evt;
  }

  /** Record a judgment: which rule fired and why. */
  decision(node: string, rule: string, because: string, data?: Record<string, unknown>): void {
    this.event('DECISION', `${rule}: ${because}`, node, { rule, because, ...data });
  }
}

/** JSONL file sink — one JSON object per line, safe to tail and replay. */
export function jsonlFileSink(path: string): EventSink {
  mkdirSync(dirname(path), { recursive: true });
  return (event: LogEvent) => {
    appendFileSync(path, `${JSON.stringify(event)}\n`);
  };
}
