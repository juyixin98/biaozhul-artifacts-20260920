/**
 * 启动配置：全部来自环境变量，带默认值与启动期校验。
 * 限额（maxRanges / maxResponseBytes）是 Range 处理的硬性策略边界。
 */

export interface ServerConfig {
  readonly host: string;
  readonly port: number;
  /** SQLite 数据库文件路径；':memory:' 表示纯内存库（测试用）。 */
  readonly dbPath: string;
  /** 单个请求允许的最大 Range 规格数量（解析后、合并前计数）。 */
  readonly maxRanges: number;
  /** 单次 Range 响应允许返回的最大字节总量（合并后求和）。 */
  readonly maxResponseBytes: number;
  /** 诊断记录环形缓冲容量。 */
  readonly diagnosticsCapacity: number;
}

const DEFAULTS: ServerConfig = {
  host: '127.0.0.1',
  port: 3000,
  dbPath: 'data/objects.db',
  maxRanges: 16,
  maxResponseBytes: 64 * 1024 * 1024,
  diagnosticsCapacity: 500,
};

function parsePositiveInt(name: string, raw: string | undefined, fallback: number): number {
  if (raw === undefined || raw === '') return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`配置项 ${name} 必须是正整数，实际值: ${JSON.stringify(raw)}`);
  }
  return value;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): ServerConfig {
  return {
    host: env.RANGE_HOST ?? DEFAULTS.host,
    port: parsePositiveInt('RANGE_PORT', env.RANGE_PORT, DEFAULTS.port),
    dbPath: env.RANGE_DB_PATH ?? DEFAULTS.dbPath,
    maxRanges: parsePositiveInt('RANGE_MAX_RANGES', env.RANGE_MAX_RANGES, DEFAULTS.maxRanges),
    maxResponseBytes: parsePositiveInt(
      'RANGE_MAX_RESPONSE_BYTES',
      env.RANGE_MAX_RESPONSE_BYTES,
      DEFAULTS.maxResponseBytes,
    ),
    diagnosticsCapacity: parsePositiveInt(
      'RANGE_DIAGNOSTICS_CAPACITY',
      env.RANGE_DIAGNOSTICS_CAPACITY,
      DEFAULTS.diagnosticsCapacity,
    ),
  };
}
