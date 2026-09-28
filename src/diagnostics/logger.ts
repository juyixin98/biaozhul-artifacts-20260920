/**
 * Minimal structured diagnostics logger.
 *
 * Every line carries a correlation id (request/call/operation) and a decision.
 * Callers MUST pass only redacted fields; this module never serializes method
 * params itself, so a secret cannot flow into stdout through logging here.
 */

export interface DiagnosticsLogger {
  accepted(event: LogEvent): void;
  rejected(event: LogEvent): void;
  undecidable(event: LogEvent): void;
  warn(event: LogEvent): void;
}

export interface LogEvent {
  readonly requestCorr?: string;
  readonly callCorr?: string;
  readonly opSeq?: number;
  readonly method?: string;
  readonly position?: number;
  readonly decision: string;
  readonly category?: string;
  readonly detail?: Record<string, unknown>;
}

function write(level: string, event: LogEvent): void {
  const line = JSON.stringify({
    ts: new Date().toISOString(),
    level,
    ...event,
  });
  process.stdout.write(`${line}\n`);
}

export class JsonDiagnosticsLogger implements DiagnosticsLogger {
  accepted(event: LogEvent): void {
    write("accepted", event);
  }
  rejected(event: LogEvent): void {
    write("rejected", event);
  }
  undecidable(event: LogEvent): void {
    write("undecidable", event);
  }
  warn(event: LogEvent): void {
    write("warn", event);
  }
}

export const noopLogger: DiagnosticsLogger = {
  accepted() {},
  rejected() {},
  undecidable() {},
  warn() {},
};
