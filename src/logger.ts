/**
 * Request-correlated diagnostic logger.
 *
 * Emits one JSON object per line (JSONL). Every event carries a request id so
 * a patch's lifecycle — receipt, version check, per-op steps, commit or
 * rollback — can be reconstructed by grepping a single id.
 *
 * Failures and uncertain outcomes are logged with `level: "warn"|"error"` and
 * a dedicated `failure` object; they are never mixed into success records.
 */

import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

export interface LogEntry {
  readonly level: LogLevel;
  readonly event: string;
  readonly requestId?: string;
  readonly [field: string]: unknown;
}

export interface Logger {
  log(entry: LogEntry): void;
  child(bound: Record<string, unknown>): Logger;
}

export function createLogger(opts: { filePath: string; toStdout: boolean }): Logger {
  mkdirSync(dirname(opts.filePath), { recursive: true });

  const write = (entry: LogEntry, extra: Record<string, unknown>): void => {
    const record = {
      ts: new Date().toISOString(),
      ...extra,
      ...entry,
    };
    const line = JSON.stringify(record) + '\n';
    appendFileSync(opts.filePath, line, { encoding: 'utf8' });
    if (opts.toStdout) process.stdout.write(line);
  };

  const make = (bound: Record<string, unknown>): Logger => ({
    log(entry: LogEntry): void {
      write(entry, bound);
    },
    child(extra: Record<string, unknown>): Logger {
      return make({ ...bound, ...extra });
    },
  });

  return make({});
}
