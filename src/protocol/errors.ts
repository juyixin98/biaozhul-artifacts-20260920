/**
 * Error taxonomy.
 *
 * Every failure crossing a module boundary is represented by a {@link MultipartError}
 * carrying a stable {@link ErrorCode}. The four top-level {@link ErrorClass}es are
 * intentionally distinguishable:
 *
 *  - INPUT_ERROR    — malformed client data / rejected policy (4xx)
 *  - STATE_CONFLICT — lifecycle violation (commit before end, double finish, aborted use)
 *  - RESOURCE_LIMIT — configured quota exhausted (size / header / part count / disk)
 *  - COMPUTE_FAILURE — unexpected I/O or OS failure (5xx), never raised for bad input
 */

export type ErrorClass =
  | 'INPUT_ERROR'
  | 'STATE_CONFLICT'
  | 'RESOURCE_LIMIT'
  | 'COMPUTE_FAILURE';

export const ErrorCode = {
  // INPUT_ERROR
  MALFORMED_CONTENT_TYPE: 'MALFORMED_CONTENT_TYPE',
  MISSING_BOUNDARY: 'MISSING_BOUNDARY',
  MALFORMED_PART_HEADERS: 'MALFORMED_PART_HEADERS',
  MALFORMED_HEADER_SYNTAX: 'MALFORMED_HEADER_SYNTAX',
  HEADER_NAME_NOT_TOKEN: 'HEADER_NAME_NOT_TOKEN',
  DUPLICATE_CONTENT_DISPOSITION: 'DUPLICATE_CONTENT_DISPOSITION',
  DUPLICATE_CONTENT_TYPE: 'DUPLICATE_CONTENT_TYPE',
  MISSING_DISPOSITION_NAME: 'MISSING_DISPOSITION_NAME',
  EMPTY_FIELD_NAME: 'EMPTY_FIELD_NAME',
  UNSUPPORTED_DISPOSITION: 'UNSUPPORTED_DISPOSITION',
  DUPLICATE_PART_NAME: 'DUPLICATE_PART_NAME',
  UNSUPPORTED_ENCODING: 'UNSUPPORTED_ENCODING',
  MALFORMED_FILENAME_ENCODING: 'MALFORMED_FILENAME_ENCODING',
  PATH_TRAVERSAL_FILENAME: 'PATH_TRAVERSAL_FILENAME',
  FILE_TYPE_REJECTED: 'FILE_TYPE_REJECTED',
  NO_FILE_PARTS: 'NO_FILE_PARTS',
  MALFORMED_BOUNDARY_DELIMITER: 'MALFORMED_BOUNDARY_DELIMITER',
  PROLOGUE_NOT_EMPTY: 'PROLOGUE_NOT_EMPTY',
  MISSING_TERMINATING_BOUNDARY: 'MISSING_TERMINATING_BOUNDARY',
  TRUNCATED_BODY: 'TRUNCATED_BODY',
  BODY_NOT_FULLY_CONSUMED: 'BODY_NOT_FULLY_CONSUMED',
  // STATE_CONFLICT
  PARSER_FINISHED: 'PARSER_FINISHED',
  PARSER_ABORTED: 'PARSER_ABORTED',
  PART_NOT_OPEN: 'PART_NOT_OPEN',
  COMMIT_BEFORE_COMPLETION: 'COMMIT_BEFORE_COMPLETION',
  ALREADY_COMMITTED: 'ALREADY_COMMITTED',
  UPLOAD_CANCELED: 'UPLOAD_CANCELED',
  // RESOURCE_LIMIT
  PART_SIZE_EXCEEDED: 'PART_SIZE_EXCEEDED',
  TOTAL_SIZE_EXCEEDED: 'TOTAL_SIZE_EXCEEDED',
  HEADER_SIZE_EXCEEDED: 'HEADER_SIZE_EXCEEDED',
  MAX_PARTS_EXCEEDED: 'MAX_PARTS_EXCEEDED',
  MAX_FIELDS_EXCEEDED: 'MAX_FIELDS_EXCEEDED',
  MAX_FILES_EXCEEDED: 'MAX_FILES_EXCEEDED',
  DISK_SPACE: 'DISK_SPACE',
  // COMPUTE_FAILURE
  IO_FAILURE: 'IO_FAILURE',
  SQLITE_FAILURE: 'SQLITE_FAILURE',
  INTERNAL: 'INTERNAL'
} as const;

export type ErrorCode = (typeof ErrorCode)[keyof typeof ErrorCode];

export interface ErrorDetail {
  [key: string]: string | number | boolean | string[] | number[] | null | undefined;
}

const ERROR_CLASS: Record<ErrorCode, ErrorClass> = {
  MALFORMED_CONTENT_TYPE: 'INPUT_ERROR',
  MISSING_BOUNDARY: 'INPUT_ERROR',
  MALFORMED_PART_HEADERS: 'INPUT_ERROR',
  MALFORMED_HEADER_SYNTAX: 'INPUT_ERROR',
  HEADER_NAME_NOT_TOKEN: 'INPUT_ERROR',
  DUPLICATE_CONTENT_DISPOSITION: 'INPUT_ERROR',
  DUPLICATE_CONTENT_TYPE: 'INPUT_ERROR',
  MISSING_DISPOSITION_NAME: 'INPUT_ERROR',
  EMPTY_FIELD_NAME: 'INPUT_ERROR',
  UNSUPPORTED_DISPOSITION: 'INPUT_ERROR',
  DUPLICATE_PART_NAME: 'INPUT_ERROR',
  UNSUPPORTED_ENCODING: 'INPUT_ERROR',
  MALFORMED_FILENAME_ENCODING: 'INPUT_ERROR',
  PATH_TRAVERSAL_FILENAME: 'INPUT_ERROR',
  FILE_TYPE_REJECTED: 'INPUT_ERROR',
  NO_FILE_PARTS: 'INPUT_ERROR',
  MALFORMED_BOUNDARY_DELIMITER: 'INPUT_ERROR',
  PROLOGUE_NOT_EMPTY: 'INPUT_ERROR',
  MISSING_TERMINATING_BOUNDARY: 'INPUT_ERROR',
  TRUNCATED_BODY: 'INPUT_ERROR',
  BODY_NOT_FULLY_CONSUMED: 'INPUT_ERROR',
  PARSER_FINISHED: 'STATE_CONFLICT',
  PARSER_ABORTED: 'STATE_CONFLICT',
  PART_NOT_OPEN: 'STATE_CONFLICT',
  COMMIT_BEFORE_COMPLETION: 'STATE_CONFLICT',
  ALREADY_COMMITTED: 'STATE_CONFLICT',
  UPLOAD_CANCELED: 'STATE_CONFLICT',
  PART_SIZE_EXCEEDED: 'RESOURCE_LIMIT',
  TOTAL_SIZE_EXCEEDED: 'RESOURCE_LIMIT',
  HEADER_SIZE_EXCEEDED: 'RESOURCE_LIMIT',
  MAX_PARTS_EXCEEDED: 'RESOURCE_LIMIT',
  MAX_FIELDS_EXCEEDED: 'RESOURCE_LIMIT',
  MAX_FILES_EXCEEDED: 'RESOURCE_LIMIT',
  DISK_SPACE: 'RESOURCE_LIMIT',
  IO_FAILURE: 'COMPUTE_FAILURE',
  SQLITE_FAILURE: 'COMPUTE_FAILURE',
  INTERNAL: 'COMPUTE_FAILURE'
};

export const HTTP_STATUS: Record<ErrorClass, number> = {
  INPUT_ERROR: 400,
  STATE_CONFLICT: 409,
  RESOURCE_LIMIT: 413,
  COMPUTE_FAILURE: 500
};

export class MultipartError extends Error {
  readonly code: ErrorCode;
  readonly errorClass: ErrorClass;
  readonly details: ErrorDetail;
  readonly httpStatus: number;
  /** offset into the request body at which the parser detected the problem, when known */
  readonly offset?: number;

  constructor(code: ErrorCode, message: string, details: ErrorDetail = {}, offset?: number) {
    super(message);
    this.name = 'MultipartError';
    this.code = code;
    this.errorClass = ERROR_CLASS[code];
    this.details = details;
    this.httpStatus = HTTP_STATUS[this.errorClass];
    if (offset !== undefined) this.offset = offset;
  }

  toJSON(): Record<string, unknown> {
    return {
      error: this.code,
      errorClass: this.errorClass,
      message: this.message,
      httpStatus: this.httpStatus,
      ...(Object.keys(this.details).length > 0 ? { details: this.details } : {}),
      ...(this.offset !== undefined ? { offset: this.offset } : {})
    };
  }
}

/** Wrap a Node system error (fs, stream) into the COMPUTE_FAILURE class. */
export function wrapIoError(cause: unknown, context: string): MultipartError {
  const err = cause as NodeJS.ErrnoException;
  if (err && (err.code === 'ENOSPC' || err.code === 'EDQUOT')) {
    return new MultipartError(
      ErrorCode.DISK_SPACE,
      `disk quota exhausted while ${context}`,
      { syscall: err.syscall ?? '', errnoCode: err.code ?? '' }
    );
  }
  return new MultipartError(
    ErrorCode.IO_FAILURE,
    `I/O failure while ${context}: ${err?.message ?? String(cause)}`,
    { errnoCode: err?.code ?? 'UNKNOWN' }
  );
}
