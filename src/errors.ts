/**
 * Typed application error. Every rejection carries a stable machine-readable
 * code and an HTTP status so failures are reported truthfully and uniformly.
 */
export type AppErrorCode =
  | "BAD_REQUEST"
  | "INVALID_BASE64"
  | "UNSUPPORTED_CID"
  | "UNSUPPORTED_ENCODING"
  | "UNSUPPORTED_MULTIHASH"
  | "UNSUPPORTED_CODEC"
  | "CONTENT_MISMATCH"
  | "BLOCK_TOO_LARGE"
  | "BLOCK_NOT_FOUND"
  | "INVALID_JSON"
  | "INVALID_METADATA"
  | "DAG_CYCLE"
  | "NOT_FOUND"
  | "VERSION_CONFLICT"
  | "CHAIN_CONFLICT"
  | "DEEP_REORG_REJECTED";

const STATUS: Record<AppErrorCode, number> = {
  BAD_REQUEST: 400,
  INVALID_BASE64: 400,
  UNSUPPORTED_CID: 400,
  UNSUPPORTED_ENCODING: 400,
  UNSUPPORTED_MULTIHASH: 422,
  UNSUPPORTED_CODEC: 422,
  CONTENT_MISMATCH: 422,
  BLOCK_TOO_LARGE: 413,
  BLOCK_NOT_FOUND: 404,
  INVALID_JSON: 422,
  INVALID_METADATA: 422,
  DAG_CYCLE: 422,
  NOT_FOUND: 404,
  VERSION_CONFLICT: 409,
  CHAIN_CONFLICT: 409,
  DEEP_REORG_REJECTED: 409,
};

export class AppError extends Error {
  readonly code: AppErrorCode;
  readonly statusCode: number;
  readonly details?: unknown;

  constructor(code: AppErrorCode, message: string, details?: unknown) {
    super(message);
    this.name = "AppError";
    this.code = code;
    this.statusCode = STATUS[code];
    this.details = details;
  }
}
