import type { RangeLimits } from '../types.js';

export interface AppConfig {
  host: string;
  port: number;
  databasePath: string;
  logPath: string | null;
  limits: RangeLimits;
  /** Emit body bytes at INFO when true — kept false by default (sensitive). */
  logBodies: boolean;
}

const DEFAULT_LIMITS: RangeLimits = {
  maxSpecs: 50,
  // Generous cap; multipart framing overhead is budgeted against it too.
  maxResponseBytes: 16 * 1024 * 1024,
  mergeGap: 1,
};

function intFromEnv(env: NodeJS.ProcessEnv, name: string, fallback: number): number {
  const raw = env[name];
  if (raw === undefined || raw === '') return fallback;
  const value = Number.parseInt(raw, 10);
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(`Invalid ${name}=${raw}: expected non-negative safe integer`);
  }
  return value;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const limits: RangeLimits = {
    maxSpecs: intFromEnv(env, 'RANGE_MAX_SPECS', DEFAULT_LIMITS.maxSpecs),
    maxResponseBytes: intFromEnv(
      env,
      'RANGE_MAX_RESPONSE_BYTES',
      DEFAULT_LIMITS.maxResponseBytes,
    ),
    mergeGap: intFromEnv(env, 'RANGE_MERGE_GAP', DEFAULT_LIMITS.mergeGap),
  };
  if (limits.maxSpecs < 1) throw new Error('RANGE_MAX_SPECS must be >= 1');
  return {
    host: env.HOST ?? '127.0.0.1',
    port: intFromEnv(env, 'PORT', 3000),
    databasePath: env.DB_PATH ?? 'data/objects.db',
    // Unset -> default file; explicitly empty -> JSONL disabled.
    logPath:
      env.DIAGNOSTICS_LOG === undefined
        ? 'data/requests.jsonl'
        : env.DIAGNOSTICS_LOG === ''
          ? null
          : env.DIAGNOSTICS_LOG,
    logBodies: env.LOG_BODIES === '1',
    limits,
  };
}
