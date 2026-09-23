/** Service limits and chain parameters, overridable via environment. */
export interface Config {
  host: string;
  port: number;
  dbPath: string;
  logLevel: string;
  /** Any single stored block larger than this is rejected. */
  maxBlockSize: number;
  /** Assembled metadata JSON payload cap. */
  maxMetadataBytes: number;
  /** Assembled media payload cap. */
  maxMediaBytes: number;
  /** Max dag-pb link depth during traversal. */
  maxDagDepth: number;
  /** Max links on a single node. */
  maxLinksPerNode: number;
  /** Number of chain heights before a proposed update becomes effective. */
  confirmations: number;
}

function intEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n) || n <= 0) throw new Error(`environment ${name} must be a positive integer, got ${raw}`);
  return n;
}

export function loadConfig(): Config {
  return {
    host: process.env.HOST ?? '127.0.0.1',
    port: intEnv('PORT', 3000),
    dbPath: process.env.DB_PATH ?? './data/nft.db',
    logLevel: process.env.LOG_LEVEL ?? 'info',
    maxBlockSize: intEnv('MAX_BLOCK_SIZE', 1 * 1024 * 1024),
    maxMetadataBytes: intEnv('MAX_METADATA_BYTES', 256 * 1024),
    maxMediaBytes: intEnv('MAX_MEDIA_BYTES', 2 * 1024 * 1024),
    maxDagDepth: intEnv('MAX_DAG_DEPTH', 16),
    maxLinksPerNode: intEnv('MAX_LINKS_PER_NODE', 1024),
    confirmations: intEnv('CONFIRMATIONS', 3),
  };
}
