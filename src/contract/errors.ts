/**
 * Typed failure categories shared across contract parsing, kernel and
 * transport. Every failure path maps to a distinct category — unknown or
 * unexpected states are never folded into a success response.
 */

export type FailureCode =
  | 'MALFORMED_CONDITION_HEADER'
  | 'INVALID_JSON_BODY'
  | 'PRECONDITION_IF_MATCH_FAILED'
  | 'PRECONDITION_IF_NONE_MATCH_FAILED'
  | 'PRECONDITION_IF_UNMODIFIED_SINCE_FAILED'
  | 'NOT_MODIFIED'
  | 'RESOURCE_NOT_FOUND'
  | 'METHOD_NOT_ALLOWED'
  | 'INTERNAL_ERROR';

export interface FailureDetail {
  readonly code: FailureCode;
  readonly message: string;
}

export class DomainError extends Error {
  readonly code: FailureCode;
  readonly statusCode: number;

  constructor(code: FailureCode, statusCode: number, message: string) {
    super(message);
    this.name = new.target.name;
    this.code = code;
    this.statusCode = statusCode;
  }
}

/** Malformed If-Match / If-None-Match / date validator header (RFC 9110: 400). */
export class MalformedConditionError extends DomainError {
  constructor(headerName: string, rawValue: string, reason: string) {
    super(
      'MALFORMED_CONDITION_HEADER',
      400,
      `Malformed ${headerName} header "${rawValue}": ${reason}`
    );
  }
}

export class InvalidJsonError extends DomainError {
  constructor(reason: string) {
    super('INVALID_JSON_BODY', 400, `Invalid JSON body: ${reason}`);
  }
}

export class PreconditionFailedError extends DomainError {
  constructor(
    code: FailureCode,
    message: string,
    readonly trace: readonly TraceStep[],
    readonly currentValidator: { readonly etag: string; readonly lastModified: string } | null = null
  ) {
    super(code, 412, message);
  }
}

export class NotModifiedError extends DomainError {
  constructor(readonly trace: readonly TraceStep[]) {
    super('NOT_MODIFIED', 304, 'Not Modified');
  }
}

export class NotFoundError extends DomainError {
  constructor(message = 'Resource not found') {
    super('RESOURCE_NOT_FOUND', 404, message);
  }
}

/** One evaluated precondition step, used for diagnostics and response logging. */
export interface TraceStep {
  readonly header: 'If-Match' | 'If-None-Match' | 'If-Modified-Since' | 'If-Unmodified-Since';
  readonly comparison: 'strong' | 'weak' | 'date';
  readonly expected: string;
  readonly actual: string;
  readonly result: 'match' | 'no-match' | 'ignored' | 'not-present';
  readonly basis: string;
}
