import { randomUUID } from 'node:crypto';
import { appendFile, mkdir } from 'node:fs/promises';
import { dirname } from 'node:path';

/**
 * Structured diagnostic records.
 *
 * Every Range decision produces one JSON record carrying a request id and
 * the key state behind it: why a request was accepted, rejected, or could
 * not be judged. Records fan out to:
 *   - a bounded in-memory ring (served by GET /diagnostics), and
 *   - an append-only JSONL file when configured.
 *
 * Sensitive-data rule: object payloads are NEVER logged by default. With
 * LOG_BODIES=1 only a short, redacted preview is attached (see
 * redactPreview); header values are length-capped.
 */

export type DecisionLabel =
  | 'accepted:single'
  | 'accepted:multipart'
  | 'accepted:full'
  | 'rejected'
  | 'indeterminate';

export interface DiagnosticRecord {
  ts: string;
  requestId: string;
  method: string;
  url: string;
  objectId: string | null;
  objectSize: number | null;
  decision: DecisionLabel;
  status: number;
  /** Machine-readable code, e.g. UNSATISFIABLE_RANGE / IF_RANGE_MISMATCH. */
  code: string;
  /** Human-readable "why". */
  reason: string;
  rangeHeader: string | null;
  specCount: number | null;
  intervals: Array<{ start: number; end: number }>;
  /** Requested byte total across accepted specs before merging. */
  requestedBytes: number | null;
  servedBytes: number | null;
  mergeCount: number | null;
  dropped: Array<{ index: number; raw: string; reason: string }>;
  bodyPreview?: string;
}

export interface DiagnosticsQuery {
  limit?: number;
  objectId?: string;
  decision?: DecisionLabel;
}

const MAX_HEADER_LOG = 512;
const MAX_PREVIEW = 128;

function cap(value: string, max: number): string {
  return value.length <= max ? value : `${value.slice(0, max)}…(${value.length} chars)`;
}

/**
 * Render a byte buffer as a masked structural preview. Content bytes are
 * NEVER revealed, even with LOG_BODIES=1: letters/digits become `#`,
 * whitespace becomes `·`-ish spacing markers, other printable punctuation
 * stays (it carries no secret entropy) and non-printable bytes become
 * `×`. The result therefore shows shape/length only — a token such as
 * `SECRET-token-xyz123` renders as `######-#####-######`.
 */
export function redactPreview(data: Buffer, max = MAX_PREVIEW): string {
  let out = '';
  const n = Math.min(data.length, max);
  for (let i = 0; i < n; i++) {
    const b = data[i]!;
    if (
      (b >= 0x30 && b <= 0x39) || // 0-9
      (b >= 0x41 && b <= 0x5a) || // A-Z
      (b >= 0x61 && b <= 0x7a) // a-z
    ) {
      out += '#';
    } else if (b === 0x20 || b === 0x09) {
      out += ' ';
    } else if (b >= 0x21 && b <= 0x7e) {
      out += String.fromCharCode(b); // punctuation only
    } else {
      out += '×';
    }
  }
  return data.length > n ? `${out}…(${data.length} bytes total)` : out;
}

export interface DiagnosticsSink {
  record(entry: DiagnosticRecord): void;
}

/** Bounded in-memory history, newest kept. */
export class MemoryRingSink implements DiagnosticsSink {
  private entries: DiagnosticRecord[] = [];

  constructor(private readonly capacity = 1000) {}

  record(entry: DiagnosticRecord): void {
    this.entries.push(entry);
    if (this.entries.length > this.capacity) {
      this.entries = this.entries.slice(this.entries.length - this.capacity);
    }
  }

  query(query: DiagnosticsQuery = {}): DiagnosticRecord[] {
    let rows = [...this.entries].reverse(); // newest first
    if (query.objectId !== undefined) {
      rows = rows.filter((r) => r.objectId === query.objectId);
    }
    if (query.decision !== undefined) {
      rows = rows.filter((r) => r.decision === query.decision);
    }
    const limit = query.limit ?? 100;
    return rows.slice(0, Math.max(0, Math.min(limit, this.capacity)));
  }
}

/** Fans a record out to several sinks; one failing sink never breaks others. */
export class CompositeSink implements DiagnosticsSink {
  constructor(private readonly sinks: DiagnosticsSink[]) {}

  record(entry: DiagnosticRecord): void {
    const safe: DiagnosticRecord = {
      ...entry,
      rangeHeader:
        entry.rangeHeader === null ? null : cap(entry.rangeHeader, MAX_HEADER_LOG),
      ...(entry.bodyPreview === undefined
        ? {}
        : { bodyPreview: cap(entry.bodyPreview, MAX_PREVIEW) }),
    };
    for (const sink of this.sinks) {
      try {
        sink.record(safe);
      } catch {
        // A diagnostics failure must not affect the response path.
      }
    }
  }
}

/** Append-only JSONL file sink with serialised writes and flush(). */
export class FileSink implements DiagnosticsSink {
  private queue: Promise<void> = Promise.resolve();

  constructor(private readonly filePath: string) {}

  record(entry: DiagnosticRecord): void {
    const line = JSON.stringify(entry);
    this.queue = this.queue
      .then(async () => {
        await mkdir(dirname(this.filePath), { recursive: true });
        await appendFile(this.filePath, `${line}\n`);
      })
      .catch(() => {
        // Swallow: diagnostics must never crash request handling.
      });
  }

  async flush(): Promise<void> {
    await this.queue;
  }
}

/** Sink that mirrors records to a console-style logger (server stdout). */
export class StreamSink implements DiagnosticsSink {
  constructor(private readonly write: (line: string) => void) {}

  record(entry: DiagnosticRecord): void {
    this.write(`[diag] ${entry.requestId} ${entry.decision} ${entry.status} ${entry.code} ${entry.reason}`);
  }
}

export function newRequestId(): string {
  return `${Date.now().toString(36)}-${randomUUID().slice(0, 8)}`;
}
