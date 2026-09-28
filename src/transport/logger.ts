/**
 * Structured run logging.
 *
 * Each request gets its own RequestRunLogger bound to a run id (echoed as
 * `X-Request-Id`). Because the binding is an explicit object passed through
 * the request rather than a global, concurrent requests can never mix run
 * identities in the logs. The kernel emits an ordered condition trace; each
 * evaluated step is logged with the validator values compared and the rule
 * (basis) that decided the result. Failures are logged at warn/error with
 * their category — never reported as success.
 */
import { randomUUID } from 'node:crypto';
import { TraceStep } from '../contract/errors.js';

export interface RunLogger {
  readonly runId: string;
  info(event: string, fields?: Record<string, unknown>): void;
  warn(event: string, fields?: Record<string, unknown>): void;
  error(event: string, fields?: Record<string, unknown>): void;
}

export interface LogEntry {
  readonly ts: string;
  readonly level: 'info' | 'warn' | 'error';
  readonly event: string;
  readonly runId: string;
  readonly fields: Record<string, unknown>;
}

export function newRunId(): string {
  // 12 hex chars is plenty for local correlation and keeps logs short.
  return randomUUID().replace(/-/g, '').slice(0, 12);
}

/** Shared JSON-line sink; optionally retains entries for test assertions. */
export class JsonRunLogger {
  private readonly buffer: LogEntry[] = [];

  constructor(
    private readonly stream: NodeJS.WritableStream = process.stdout,
    private readonly retain: boolean = false
  ) {}

  forRun(runId: string = newRunId()): RunLogger {
    return new RequestRunLogger(runId, (entry) => {
      if (this.retain) this.buffer.push(entry);
      this.stream.write(JSON.stringify(entry) + '\n');
    });
  }

  /** Buffered entries across all runs (each carries its own runId). */
  entries(): readonly LogEntry[] {
    return this.buffer;
  }
}

class RequestRunLogger implements RunLogger {
  constructor(
    readonly runId: string,
    private readonly emit: (entry: LogEntry) => void
  ) {}

  info(event: string, fields: Record<string, unknown> = {}): void {
    this.write('info', event, fields);
  }

  warn(event: string, fields: Record<string, unknown> = {}): void {
    this.write('warn', event, fields);
  }

  error(event: string, fields: Record<string, unknown> = {}): void {
    this.write('error', event, fields);
  }

  private write(level: LogEntry['level'], event: string, fields: Record<string, unknown>): void {
    this.emit({ ts: new Date().toISOString(), level, event, runId: this.runId, fields });
  }
}

/** Human-readable rendering of a condition trace for log fields. */
export function traceToLog(trace: readonly TraceStep[]): Array<Record<string, unknown>> {
  return trace.map((step, index) => ({
    step: index + 1,
    header: step.header,
    comparison: step.comparison,
    expected: step.expected,
    actual: step.actual,
    result: step.result,
    basis: step.basis
  }));
}
