/**
 * Process configuration parsed and validated once at startup.
 * Fail fast on malformed configuration instead of trusting env at use sites.
 */

export interface AppConfig {
  readonly host: string;
  readonly port: number;
  readonly dbPath: string;
  readonly logLevel: "debug" | "info" | "warn" | "error" | "silent";
}

const VALID_LOG_LEVELS = ["debug", "info", "warn", "error", "silent"] as const;
type LogLevel = (typeof VALID_LOG_LEVELS)[number];

function parsePort(raw: string | undefined): number {
  if (raw === undefined || raw === "") return 3000;
  if (!/^\d+$/.test(raw)) {
    throw new Error(`PORT must be a non-negative integer, received: ${raw}`);
  }
  const port = Number(raw);
  if (port > 65535) throw new Error(`PORT out of range: ${port}`);
  return port;
}

function parseLogLevel(raw: string | undefined): LogLevel {
  const value = (raw ?? "info").toLowerCase();
  if (!VALID_LOG_LEVELS.includes(value as LogLevel)) {
    throw new Error(
      `LOG_LEVEL must be one of ${VALID_LOG_LEVELS.join(", ")}, received: ${raw}`,
    );
  }
  return value as LogLevel;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const port = parsePort(env.PORT);
  const logLevel = parseLogLevel(env.LOG_LEVEL);
  const host = env.HOST ?? "127.0.0.1";
  const dbPath = env.DB_PATH ?? "data/service.db";
  return { host, port, dbPath, logLevel };
}
