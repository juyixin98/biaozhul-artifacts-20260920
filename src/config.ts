/**
 * Configuration layer.
 *
 * Single source of runtime settings, parsed and validated once at startup so
 * that invalid configuration fails fast instead of surfacing mid-request.
 */
import { mkdirSync } from 'node:fs';
import path from 'node:path';

export type LogLevel = 'fatal' | 'error' | 'warn' | 'info' | 'debug' | 'trace';

export interface AppConfig {
  readonly port: number;
  readonly host: string;
  readonly dbPath: string;
  readonly logLevel: LogLevel;
}

const VALID_LEVELS: readonly LogLevel[] = ['fatal', 'error', 'warn', 'info', 'debug', 'trace'];

function parsePort(value: string | undefined): number {
  if (value === undefined || value === '') return 8080;
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`Invalid PORT "${value}": expected integer 1-65535`);
  }
  return port;
}

function parseLogLevel(value: string | undefined): LogLevel {
  if (value === undefined || value === '') return 'info';
  if (!VALID_LEVELS.includes(value as LogLevel)) {
    throw new Error(`Invalid LOG_LEVEL "${value}": expected one of ${VALID_LEVELS.join(', ')}`);
  }
  return value as LogLevel;
}

/**
 * Load configuration from the process environment.
 * Defaults point at local-only SQLite storage; no external services are used.
 */
export function loadConfig(env: NodeJS.ProcessEnv = process.env, cwd: string = process.cwd()): AppConfig {
  const dbPath = path.resolve(cwd, env.DB_PATH ?? 'data/app.db');
  mkdirSync(path.dirname(dbPath), { recursive: true });
  return {
    port: parsePort(env.PORT),
    host: env.HOST ?? '127.0.0.1',
    dbPath,
    logLevel: parseLogLevel(env.LOG_LEVEL)
  };
}
