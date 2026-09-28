/**
 * 进程配置：全部来自环境变量，均有本地默认值，无需任何外部账号。
 */
import path from 'node:path';

export interface AppConfig {
  dbPath: string;
  logFile: string | null;
  ringBufferSize: number;
  host: string;
  port: number;
  /** 诊断接口是否允许跨进程外访问；本地默认仅绑定 127.0.0.1 */
  trustRequestIdHeader: boolean;
}

function intFromEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const dataDir = env.DATA_DIR ?? path.resolve(process.cwd(), 'data');
  return {
    dbPath: env.DB_PATH ?? path.join(dataDir, 'app.db'),
    logFile: env.LOG_FILE ?? null,
    ringBufferSize: intFromEnv('DIAG_RING_SIZE', 200),
    host: env.HOST ?? '127.0.0.1',
    port: intFromEnv('PORT', 8787),
    trustRequestIdHeader: env.TRUST_REQUEST_ID_HEADER === '1',
  };
}
