/**
 * Error contract shared across every layer.
 *
 * Four mutually exclusive failure classes are required by the specification so
 * that diagnostics, HTTP responses and logs can distinguish them:
 *
 *  - INPUT_ERROR     malformed wire data, protocol violations, bad user input
 *  - STATE_CONFLICT  parser/store lifecycle misuse (commit before finalize, ...)
 *  - RESOURCE_LIMIT  per-part / aggregate / header length quotas exceeded
 *  - COMPUTE_ERROR   underlying I/O or storage engine failure (fs, sqlite)
 *
 * Each error carries a stable machine-readable `code`, a human-readable
 * message, and optional structured `details` that are safe to surface to API
 * clients (no file-system paths of unrelated requests, no secrets).
 */

export type ErrorClass =
  | 'INPUT_ERROR'
  | 'STATE_CONFLICT'
  | 'RESOURCE_LIMIT'
  | 'COMPUTE_ERROR';

export type MultipartErrorCode =
  // content type / envelope
  | 'NOT_MULTIPART'
  | 'MISSING_BOUNDARY'
  | 'BOUNDARY_TOO_LONG'
  // header section
  | 'HEADER_TOO_LONG'
  | 'MALFORMED_HEADER'
  | 'MISSING_CONTENT_DISPOSITION'
  | 'DUPLICATE_HEADER'
  | 'DUPLICATE_PART_NAME'
  | 'MISSING_FIELD_NAME'
  | 'UNSUPPORTED_HEADER'
  | 'BAD_HEADER_ENCODING'
  | 'FILENAME_REJECTED'
  // body / framing
  | 'MALFORMED_BOUNDARY'
  | 'MISSING_TERMINATOR'
  | 'BARE_LF'
  | 'INVALID_BYTES'
  | 'EMPTY_BODY'
  // request routing / generic
  | 'BAD_REQUEST'
  | 'NOT_FOUND'
  // limits
  | 'PART_TOO_LARGE'
  | 'TOTAL_TOO_LARGE'
  | 'TOO_MANY_PARTS'
  | 'FIELD_TOO_LARGE'
  // state
  | 'PARSER_FINISHED'
  | 'PART_NOT_OPEN'
  | 'NOT_TERMINATED'
  | 'ALREADY_COMMITTED'
  | 'ALREADY_ABORTED'
  // storage / compute
  | 'TEMP_IO_ERROR'
  | 'DB_ERROR';

const ERROR_CLASS_STATUS: Record<ErrorClass, number> = {
  INPUT_ERROR: 400,
  STATE_CONFLICT: 409,
  RESOURCE_LIMIT: 413,
  COMPUTE_ERROR: 500,
};

export class MultipartError extends Error {
  readonly errorClass: ErrorClass;
  readonly code: MultipartErrorCode;
  readonly details: Record<string, unknown>;

  constructor(
    errorClass: ErrorClass,
    code: MultipartErrorCode,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'MultipartError';
    this.errorClass = errorClass;
    this.code = code;
    this.details = details;
  }

  /** HTTP status canonical for this failure class. */
  get httpStatus(): number {
    return ERROR_CLASS_STATUS[this.errorClass];
  }

  /** Stable JSON shape returned by the diagnostics/HTTP layer. */
  toJSON(): {
    errorClass: ErrorClass;
    code: MultipartErrorCode;
    message: string;
    details: Record<string, unknown>;
  } {
    return {
      errorClass: this.errorClass,
      code: this.code,
      message: this.message,
      details: this.details,
    };
  }
}

/** Type guard used by the HTTP layer and tests. */
export function isMultipartError(value: unknown): value is MultipartError {
  return value instanceof MultipartError;
}

export function inputError(
  code: MultipartErrorCode,
  message: string,
  details?: Record<string, unknown>,
): MultipartError {
  return new MultipartError('INPUT_ERROR', code, message, details);
}

export function stateError(
  code: MultipartErrorCode,
  message: string,
  details?: Record<string, unknown>,
): MultipartError {
  return new MultipartError('STATE_CONFLICT', code, message, details);
}

export function resourceError(
  code: MultipartErrorCode,
  message: string,
  details?: Record<string, unknown>,
): MultipartError {
  return new MultipartError('RESOURCE_LIMIT', code, message, details);
}

export function computeError(
  code: MultipartErrorCode,
  message: string,
  details?: Record<string, unknown>,
): MultipartError {
  return new MultipartError('COMPUTE_ERROR', code, message, details);
}
