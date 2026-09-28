/**
 * Process configuration, read once from environment with local-safe defaults.
 */
export interface AppConfig {
  port: number;
  host: string;
  dbPath: string;
  logPath: string;
  defaultTimeoutMs: number;
  maxTimeoutMs: number;
}

function numberEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) throw new Error(`env ${name} must be a number, got: ${raw}`);
  return parsed;
}

export const config: AppConfig = {
  port: numberEnv('PORT', 3000),
  host: process.env.HOST ?? '127.0.0.1',
  dbPath: process.env.DB_PATH ?? 'data/runs.sqlite',
  logPath: process.env.LOG_PATH ?? 'logs/runs.jsonl',
  defaultTimeoutMs: numberEnv('DEFAULT_TIMEOUT_MS', 300),
  maxTimeoutMs: numberEnv('MAX_TIMEOUT_MS', 5000),
};
