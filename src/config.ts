/**
 * Central configuration. Everything environment-specific lives here with a
 * safe local default — no secrets, no production accounts. Override via env.
 */
function intFromEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) ? n : fallback;
}

export const config = {
  /** HTTP bind */
  host: process.env.HOST ?? '127.0.0.1',
  port: intFromEnv('PORT', 3000),
  logLevel: process.env.LOG_LEVEL ?? 'info',

  /** SQLite file (use :memory: for ephemeral runs) */
  dbPath: process.env.DB_PATH ?? './data/diffs.db',

  /** parser / analyzer safety bounds */
  maxRefHops: intFromEnv('MAX_REF_HOPS', 32),
  /** bound on witness sample nesting depth */
  maxWitnessDepth: intFromEnv('MAX_WITNESS_DEPTH', 6),
  /** maximum accepted request document size (bytes) */
  maxDocumentBytes: intFromEnv('MAX_DOCUMENT_BYTES', 2 * 1024 * 1024),

  /** supported OpenAPI dialect prefix */
  supportedSpecPattern: /^3\.1\.\d+$/,
} as const;

export type AppConfig = typeof config;
