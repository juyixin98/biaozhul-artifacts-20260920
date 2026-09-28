/**
 * Process configuration, resolvable exclusively from local environment
 * variables (12-factor style). No network services are required.
 */

import { DEFAULT_FILE_POLICY, DEFAULT_LIMITS, type FilePolicy, type Limits } from './protocol/types.js';

export interface AppConfig {
  port: number;
  host: string;
  dataDir: string;
  tmpDir: string;
  filesDir: string;
  dbPath: string;
  runLogPath: string;
  limits: Limits;
  filePolicy: FilePolicy;
  /** when true, a submission must contain at least one file part */
  requireFilePart: boolean;
}

function intEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const v = Number.parseInt(raw, 10);
  if (!Number.isFinite(v) || v <= 0) {
    throw new Error(`environment variable ${name} must be a positive integer, got ${JSON.stringify(raw)}`);
  }
  return v;
}

export function loadConfig(): AppConfig {
  const dataDir = process.env.DATA_DIR ?? 'data';
  const limits: Limits = {
    maxFieldSize: intEnv('LIMIT_FIELD', DEFAULT_LIMITS.maxFieldSize),
    maxFileSize: intEnv('LIMIT_FILE', DEFAULT_LIMITS.maxFileSize),
    maxTotalSize: intEnv('LIMIT_TOTAL', DEFAULT_LIMITS.maxTotalSize),
    maxHeaderSize: intEnv('LIMIT_HEADER', DEFAULT_LIMITS.maxHeaderSize),
    maxParts: intEnv('LIMIT_MAX_PARTS', DEFAULT_LIMITS.maxParts),
    maxFields: intEnv('LIMIT_MAX_FIELDS', DEFAULT_LIMITS.maxFields),
    maxFiles: intEnv('LIMIT_MAX_FILES', DEFAULT_LIMITS.maxFiles)
  };
  if (limits.maxTotalSize < limits.maxFileSize) {
    throw new Error('LIMIT_TOTAL must be >= LIMIT_FILE');
  }
  return {
    port: intEnv('PORT', 3000),
    host: process.env.HOST ?? '127.0.0.1',
    dataDir,
    tmpDir: process.env.TMP_DIR ?? `${dataDir}/tmp`,
    filesDir: process.env.FILES_DIR ?? `${dataDir}/files`,
    dbPath: process.env.DB_PATH ?? `${dataDir}/app.sqlite`,
    runLogPath: process.env.RUN_LOG_PATH ?? `${dataDir}/run-log.jsonl`,
    limits,
    filePolicy: { ...DEFAULT_FILE_POLICY },
    requireFilePart: process.env.REQUIRE_FILE_PART !== 'false'
  };
}
