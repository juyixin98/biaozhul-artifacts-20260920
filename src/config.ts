/**
 * Configuration layer.
 *
 * All tunables come from environment variables with explicit defaults. Values
 * are validated at startup so a bad configuration fails fast instead of
 * surfacing as an unknown runtime error later.
 */

export interface AppConfig {
  /** SQLite database file path, or ":memory:" for an ephemeral database. */
  readonly databasePath: string;
  /** TCP port the HTTP server listens on. */
  readonly port: number;
  /** Network interface the HTTP server binds to. */
  readonly host: string;
  /**
   * ETag strength used for freshly written snapshots.
   * - "strong": byte-for-byte opaque validators (W/ prefix never emitted)
   * - "weak":   weak validators, safe when representations may vary semantically
   */
  readonly etagStrength: "strong" | "weak";
  /** Enable verbose structured diagnostics logging. */
  readonly diagnostics: boolean;
  /** Milliseconds a write lock may be awaited before giving up. */
  readonly busyTimeoutMs: number;
}

export class ConfigError extends Error {
  override readonly name = "ConfigError";
}

function parsePort(raw: string | undefined, fallback: number): number {
  if (raw === undefined || raw === "") return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 1 || value > 65535) {
    throw new ConfigError(`Invalid PORT: expected integer 1..65535, got ${JSON.stringify(raw)}`);
  }
  return value;
}

function parseStrength(raw: string | undefined): "strong" | "weak" {
  if (raw === undefined || raw === "") return "strong";
  if (raw === "strong" || raw === "weak") return raw;
  throw new ConfigError(`Invalid ETAG_STRENGTH: expected "strong" or "weak", got ${JSON.stringify(raw)}`);
}

function parseBusyTimeout(raw: string | undefined): number {
  if (raw === undefined || raw === "") return 5000;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0) {
    throw new ConfigError(`Invalid BUSY_TIMEOUT_MS: expected non-negative integer, got ${JSON.stringify(raw)}`);
  }
  return value;
}

/** Build configuration from a process-style environment record. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  return {
    databasePath: env.DATABASE_PATH ?? "data/app.db",
    port: parsePort(env.PORT, 3000),
    host: env.HOST ?? "127.0.0.1",
    etagStrength: parseStrength(env.ETAG_STRENGTH),
    diagnostics: env.DIAGNOSTICS === "1" || env.DIAGNOSTICS === "true",
    busyTimeoutMs: parseBusyTimeout(env.BUSY_TIMEOUT_MS),
  };
}
