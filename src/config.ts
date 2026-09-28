/**
 * Process configuration, read once at startup. Every value has a default so
 * the service runs locally with no accounts; all knobs are environment-based.
 */

export interface AppConfig {
  host: string;
  port: number;
  /** SQLite file path, or ':memory:' for ephemeral runs/tests. */
  databasePath: string;
  logLevel: string;
  /** Reject request bodies larger than this (bytes). */
  maxBodyBytes: number;
}

function intFromEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed)) {
    throw new Error(`environment variable ${name} must be an integer, got ${JSON.stringify(raw)}`);
  }
  return parsed;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  return {
    host: env.HOST ?? '127.0.0.1',
    port: intFromEnv('PORT', 3000),
    databasePath: env.DATABASE_PATH ?? './data/patch-service.sqlite',
    logLevel: env.LOG_LEVEL ?? 'info',
    maxBodyBytes: intFromEnv('MAX_BODY_BYTES', 1_048_576),
  };
}
