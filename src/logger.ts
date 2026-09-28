/**
 * Explainability logger.
 *
 * Every diagnostic line carries the request identity (`requestId`, plus
 * document id / expected version where relevant) and a `phase` marking where
 * in the pipeline it was produced:
 *
 *   contract -> kernel(step-by-step) -> store(version handling) -> response
 *
 * Failures are emitted as structured `outcome=failure` records with a typed
 * `category`; conclusions the service cannot prove are emitted as
 * `certainty=uncertain` rather than reported as success.
 *
 * In tests the logger can write into an in-memory sink.
 */

export type Certainty = 'certain' | 'uncertain';

export interface LogFields {
  [key: string]: unknown;
}

export interface LogSink {
  write(level: LogLevel, message: string, fields: LogFields): void;
}

export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

const LEVEL_ORDER: Record<LogLevel, number> = {
  debug: 10,
  info: 20,
  warn: 30,
  error: 40,
};

export class JsonLogger {
  constructor(
    private readonly sink: LogSink,
    private readonly threshold: LogLevel = 'info',
    private readonly base: LogFields = {},
  ) {}

  child(fields: LogFields): JsonLogger {
    return new JsonLogger(this.sink, this.threshold, { ...this.base, ...fields });
  }

  debug(message: string, fields: LogFields = {}): void {
    this.log('debug', message, fields);
  }
  info(message: string, fields: LogFields = {}): void {
    this.log('info', message, fields);
  }
  warn(message: string, fields: LogFields = {}): void {
    this.log('warn', message, fields);
  }
  error(message: string, fields: LogFields = {}): void {
    this.log('error', message, fields);
  }

  private log(level: LogLevel, message: string, fields: LogFields): void {
    if (LEVEL_ORDER[level] < LEVEL_ORDER[this.threshold]) return;
    this.sink.write(level, message, { ...this.base, ...fields });
  }
}

/** NDJSON sink — one JSON object per line, suitable for production logs. */
export class StdoutJsonSink implements LogSink {
  constructor(private readonly stream: NodeJS.WritableStream = process.stdout) {}

  write(level: LogLevel, message: string, fields: LogFields): void {
    const record = {
      ts: new Date().toISOString(),
      level,
      msg: message,
      ...fields,
    };
    this.stream.write(JSON.stringify(record) + '\n');
  }
}

/** Capture sink used by tests; keeps structured records, not rendered text. */
export class MemorySink implements LogSink {
  readonly records: Array<{ level: LogLevel; message: string; fields: LogFields }> = [];

  write(level: LogLevel, message: string, fields: LogFields): void {
    this.records.push({ level, message, fields });
  }

  /** All structured records for a request id, in emission order. */
  forRequest(requestId: string): LogFields[] {
    return this.records
      .filter((r) => r.fields.requestId === requestId)
      .map((r) => ({ msg: r.message, ...r.fields }));
  }
}
