/**
 * Exhaustive, stable failure categories for patch diagnostics.
 *
 * Categories are part of the public contract: tests assert against the exact
 * string, so they must never be renamed without bumping the contract.
 */
export type ErrorCategory =
  | 'MALFORMED_PATCH'
  | 'INVALID_POINTER'
  | 'POINTER_TARGET_MISSING'
  | 'TEST_FAILURE'
  | 'POINTER_PARENT_MISSING'
  | 'ARRAY_INDEX_OUT_OF_BOUNDS'
  | 'ARRAY_INDEX_INVALID'
  | 'PATH_TYPE_MISMATCH'
  | 'MOVE_INTO_DESCENDANT'
  | 'DOCUMENT_NOT_FOUND'
  | 'AUDIT_RECORD_NOT_FOUND'
  | 'VERSION_CONFLICT'
  | 'BAD_REQUEST_BODY'
  | 'INTERNAL_ERROR';

/** Human-readable HTTP status mapping for each failure category. */
const STATUS_BY_CATEGORY: Record<ErrorCategory, number> = {
  MALFORMED_PATCH: 400,
  INVALID_POINTER: 400,
  POINTER_TARGET_MISSING: 422,
  TEST_FAILURE: 409,
  POINTER_PARENT_MISSING: 422,
  ARRAY_INDEX_OUT_OF_BOUNDS: 422,
  ARRAY_INDEX_INVALID: 400,
  PATH_TYPE_MISMATCH: 422,
  MOVE_INTO_DESCENDANT: 409,
  DOCUMENT_NOT_FOUND: 404,
  AUDIT_RECORD_NOT_FOUND: 404,
  VERSION_CONFLICT: 409,
  BAD_REQUEST_BODY: 400,
  INTERNAL_ERROR: 500,
};

/**
 * Error carrying a machine category, the step it occurred at, and a
 * human-explainable message. `details` holds structured evidence (paths,
 * indices, versions) rather than prose.
 */
export class PatchError extends Error {
  readonly category: ErrorCategory;
  readonly opIndex: number | null;
  readonly details: Record<string, unknown>;

  constructor(
    category: ErrorCategory,
    message: string,
    opIndex: number | null = null,
    details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'PatchError';
    this.category = category;
    this.opIndex = opIndex;
    this.details = details;
  }

  httpStatus(): number {
    return STATUS_BY_CATEGORY[this.category];
  }
}

export function isPatchError(value: unknown): value is PatchError {
  return value instanceof PatchError;
}
