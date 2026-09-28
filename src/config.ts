/**
 * Process configuration, read once at startup. Every value is validated at
 * the boundary (environment input) so a typo fails fast instead of silently
 * becoming a default.
 */

export interface AppConfig {
  host: string;
  port: number;
  /** SQLite database file. ':memory:' is used by the test suite. */
  dbPath: string;
  /** Upper bound a client may ask an async task to sleep (input validation). */
  maxTaskDelayMs: number;
  /** 'silent' | 'info' | 'debug'. Events are always persisted regardless. */
  logLevel: 'silent' | 'info' | 'debug';
}

function readPort(raw: string | undefined): number {
  if (raw === undefined) return 8545;
  if (!/^\d{1,5}$/.test(raw)) throw new Error(`PORT must be a port number, got ${raw}`);
  const port = Number(raw);
  if (port < 1 || port > 65535) throw new Error(`PORT out of range: ${port}`);
  return port;
}

function readLogLevel(raw: string | undefined): AppConfig['logLevel'] {
  const level = raw ?? 'info';
  if (level !== 'silent' && level !== 'info' && level !== 'debug') {
    throw new Error(`LOG_LEVEL must be silent|info|debug, got ${level}`);
  }
  return level;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const maxDelayRaw = env.TASK_MAX_DELAY_MS ?? '5000';
  if (!/^\d+$/.test(maxDelayRaw)) {
    throw new Error(`TASK_MAX_DELAY_MS must be a non-negative integer, got ${maxDelayRaw}`);
  }
  return {
    host: env.HOST ?? '127.0.0.1',
    port: readPort(env.PORT),
    dbPath: env.DB_PATH ?? './data/rpc.sqlite',
    maxTaskDelayMs: Number(maxDelayRaw),
    logLevel: readLogLevel(env.LOG_LEVEL),
  };
}
