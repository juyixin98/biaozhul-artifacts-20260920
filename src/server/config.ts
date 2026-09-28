/**
 * 集中配置（12-factor 风格，全部来自环境变量，含默认值）。
 */

export interface AppConfig {
  host: string;
  port: number;
  databaseFile: string;
  logRequests: boolean;
  logLevel: string;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  return {
    host: env.HOST ?? '127.0.0.1',
    port: Number.parseInt(env.PORT ?? '4000', 10),
    databaseFile: env.DATABASE_FILE ?? 'data/app.db',
    logRequests: (env.LOG_REQUESTS ?? 'true') !== 'false',
    logLevel: env.LOG_LEVEL ?? 'info',
  };
}
