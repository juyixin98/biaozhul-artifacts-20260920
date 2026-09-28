/**
 * Error taxonomy for the composite engine.
 *
 * Every failure inside the engine is represented by a `CompositeError` carrying a
 * stable `category` string. The four mandated top-level buckets are:
 *
 *   INPUT_ERROR      – malformed request / contract (4xx, caller's fault)
 *   STATE_CONFLICT   – persisted state disagrees with the request (409)
 *   RESOURCE_EXHAUSTED – deadline, budget or concurrency limit hit
 *   COMPUTATION_FAILED – an upstream source call failed / produced bad data
 *
 * `retryable` tells whether the same request may succeed on retry.
 */
export type ErrorCategory =
  | 'INPUT_ERROR'
  | 'STATE_CONFLICT'
  | 'RESOURCE_EXHAUSTED'
  | 'COMPUTATION_FAILED';

export interface ErrorDetail {
  /** Machine-readable stable reason code, e.g. `SOURCE_TIMEOUT`. */
  reason: string;
  message: string;
  category: ErrorCategory;
  retryable: boolean;
  /** Structured context safe to expose to API clients. */
  context?: Record<string, unknown>;
}

export class CompositeError extends Error {
  readonly detail: ErrorDetail;

  constructor(detail: ErrorDetail) {
    super(detail.message);
    this.name = 'CompositeError';
    this.detail = detail;
  }
}

export function makeError(
  category: ErrorCategory,
  reason: string,
  message: string,
  options: { retryable?: boolean; context?: Record<string, unknown> } = {},
): CompositeError {
  return new CompositeError({
    reason,
    message,
    category,
    retryable: options.retryable ?? false,
    ...(options.context !== undefined ? { context: options.context } : {}),
  });
}

export const inputError = (reason: string, message: string, context?: Record<string, unknown>) =>
  makeError('INPUT_ERROR', reason, message, { retryable: false, context });

export const stateConflict = (reason: string, message: string, context?: Record<string, unknown>) =>
  makeError('STATE_CONFLICT', reason, message, { retryable: false, context });

export const resourceExhausted = (reason: string, message: string, context?: Record<string, unknown>) =>
  makeError('RESOURCE_EXHAUSTED', reason, message, { retryable: true, context });

export const computationFailed = (
  reason: string,
  message: string,
  options: { retryable?: boolean; context?: Record<string, unknown> } = {},
) => makeError('COMPUTATION_FAILED', reason, message, options);

/** True when an unknown thrown value represents one of the four categories. */
export function isCompositeError(value: unknown): value is CompositeError {
  return value instanceof CompositeError;
}

/** Normalize anything thrown by a source into a categorized ErrorDetail. */
export function normalizeThrown(value: unknown): ErrorDetail {
  if (isCompositeError(value)) return value.detail;
  if (value instanceof Error) {
    return {
      reason: 'UNEXPECTED_SOURCE_ERROR',
      message: value.message,
      category: 'COMPUTATION_FAILED',
      retryable: false,
    };
  }
  return {
    reason: 'NON_ERROR_THROW',
    message: String(value),
    category: 'COMPUTATION_FAILED',
    retryable: false,
  };
}
