/**
 * Central configuration. Everything is environment-driven with local-safe
 * defaults; no secrets or production accounts are involved.
 */
export interface AppConfig {
  port: number;
  dbPath: string;
}

function readPort(raw: string | undefined): number {
  const n = Number(raw ?? '3000');
  if (!Number.isInteger(n) || n <= 0 || n > 65535) {
    throw new Error(`invalid PORT: ${raw}`);
  }
  return n;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  return {
    port: readPort(env['PORT']),
    dbPath: env['CONTRACT_DIFF_DB'] ?? './data/contract-diff.sqlite',
  };
}
