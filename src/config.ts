/**
 * Global limits and policy constants.
 *
 * These are deliberately conservative and explicit: every limit cited by the
 * verification evidence (block size, traversal depth, ...) comes from here.
 */
export const LIMITS = {
  /** Maximum byte length of any stored content block. */
  MAX_BLOCK_BYTES: 5 * 1024 * 1024,
  /** Maximum byte length of a JSON metadata/link block. */
  MAX_JSON_BYTES: 256 * 1024,
  /** Maximum DAG traversal depth (metadata -> links -> media). */
  MAX_DAG_DEPTH: 32,
  /** Maximum number of distinct nodes visited in one verification. */
  MAX_DAG_NODES: 4096,
  /** Supported metadata media codecs. */
  SUPPORTED_IMAGE_TYPES: ["image/png", "image/jpeg", "image/gif", "image/webp"] as const,
  /** Number of confirmations required before a revision is considered finalized. */
  REQUIRED_CONFIRMATIONS: 3,
} as const;

export type ImageType = (typeof LIMITS.SUPPORTED_IMAGE_TYPES)[number];

export interface AppConfig {
  dbPath: string;
  host: string;
  port: number;
  logLevel: string;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  return {
    dbPath: env.NFT_DB_PATH ?? "data/nft.db",
    host: env.HOST ?? "127.0.0.1",
    port: env.PORT ? Number(env.PORT) : 3000,
    logLevel: env.LOG_LEVEL ?? "info",
  };
}
