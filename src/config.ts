/**
 * Runtime configuration, validated once at process start (fail fast).
 *
 * Every value has a local default so the service runs with purely local
 * synthetic data and no external accounts.
 */

export interface AppConfig {
  readonly httpPort: number;
  readonly httpHost: string;
  /** SQLite file. ":memory:" keeps the whole service local and ephemeral. */
  readonly databasePath: string;
  /** Write one structured JSON line per diagnostic event. */
  readonly logFilePath: string;
  /** Echo structured logs to stdout as well as the log file. */
  readonly logToStdout: boolean;
  /** Pretty-print JSON bodies stored on disk (fixtures/debug convenience). */
  readonly prettyStorage: boolean;
}

function readPort(raw: string | undefined, fallback: number): number {
  if (raw === undefined || raw === '') return fallback;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 1 || n > 65535) {
    throw new Error(`Invalid PORT: expected integer 1..65535, got ${JSON.stringify(raw)}`);
  }
  return n;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const logFilePath = env.LOG_FILE ?? './logs/service.log';
  return {
    httpPort: readPort(env.PORT, 8080),
    httpHost: env.HOST ?? '127.0.0.1',
    databasePath: env.DATABASE_PATH ?? './data/service.sqlite',
    logFilePath,
    logToStdout: env.LOG_STDOUT === '1',
    prettyStorage: env.PRETTY_STORAGE === '1',
  };
}
