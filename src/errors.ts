/**
 * Error taxonomy. Every failure in the system is mapped to one of these
 * categories so that input errors, state conflicts, resource exhaustion,
 * timeouts and computation failures are always distinguishable in both
 * HTTP responses and persisted run logs.
 */
export type FailureCategory =
  | 'MISSING_INPUT' // request/input validation error (400)
  | 'NOT_FOUND' // unknown contract / run (404)
  | 'INVALID_CONTRACT' // composite contract is malformed (500 at registration, never per request)
  | 'SOURCE_FAILURE' // a data source returned an error / bad payload (upstream computation failure)
  | 'SOURCE_TIMEOUT' // a data source did not settle before the propagated deadline
  | 'CANCELLED' // work aborted because the run was cancelled after its deadline
  | 'STATE_CONFLICT' // optimistic/snapshot version conflict (409 semantics)
  | 'RESOURCE_EXHAUSTED' // local concurrency slots / queues exhausted
  | 'COMPUTATION_FAILED' // assembly/transform computation failed
  | 'CONSISTENCY_LIMITED'; // not a hard failure: snapshot could not be fully enforced

export interface ErrorInit {
  category: FailureCategory;
  code: string;
  message: string;
  httpStatus: number;
  retryable?: boolean;
  details?: Record<string, unknown>;
  causedBy?: string;
}

export class DomainError extends Error {
  readonly category: FailureCategory;
  readonly code: string;
  readonly httpStatus: number;
  readonly retryable: boolean;
  readonly details: Record<string, unknown>;
  readonly causedBy?: string;

  constructor(init: ErrorInit) {
    super(init.message);
    this.name = 'DomainError';
    this.category = init.category;
    this.code = init.code;
    this.httpStatus = init.httpStatus;
    this.retryable = init.retryable ?? false;
    this.details = init.details ?? {};
    this.causedBy = init.causedBy;
  }

  toFailure(at: string, causedBy?: string): FailureDetail {
    return {
      category: this.category,
      code: this.code,
      message: this.message,
      retryable: this.retryable,
      causedBy: causedBy ?? this.causedBy,
      at,
    };
  }
}

export interface FailureDetail {
  category: FailureCategory;
  code: string;
  message: string;
  retryable: boolean;
  causedBy?: string;
  at: string;
}

export const failureHttpStatus: Partial<Record<FailureCategory, number>> = {
  MISSING_INPUT: 400,
  NOT_FOUND: 404,
  INVALID_CONTRACT: 500,
  SOURCE_FAILURE: 502,
  SOURCE_TIMEOUT: 504,
  CANCELLED: 504,
  STATE_CONFLICT: 409,
  RESOURCE_EXHAUSTED: 503,
  COMPUTATION_FAILED: 500,
  CONSISTENCY_LIMITED: 200,
};
