import type { JsonValue } from '../contract/protocol.js';

/**
 * Method handler boundary. Handlers know nothing about JSON-RPC envelopes or
 * HTTP; they receive validated params and a context, and return JSON data.
 *
 * `sideEffect: true` marks methods that mutate domain state. Only those get
 * an independent operationId (the RPC id is never used for that) and may
 * accept a client idempotency key.
 */

export interface HandlerContext {
  operationId: string | null;
  /** Aborts when the client connection is lost. Long work MUST observe it. */
  signal: AbortSignal;
  /** Cancellable delay. Rejects with the provided reason when signal aborts. */
  sleep(ms: number): Promise<void>;
  /**
   * Ask the kernel to cancel a live detached task. Returns false when no such
   * live task exists (e.g. already finished). Injected by the kernel so that
   * method handlers never reach into process registries directly.
   */
  requestCancel(targetOperationId: string, reason: string): boolean;
}

/**
 * A detached reply: the RPC responds NOW with `reply`, while the operation
 * stays `pending` and completes later when `work` settles. Used by
 * fire-and-forget async tasks whose outcome is queryable afterwards.
 */
export interface DetachedWork {
  readonly kind: 'detached';
  reply: JsonValue;
  work: Promise<JsonValue>;
}

export function isDetachedWork(value: unknown): value is DetachedWork {
  return (
    typeof value === 'object' &&
    value !== null &&
    (value as { kind?: unknown }).kind === 'detached' &&
    'work' in value
  );
}

export type HandlerResult = JsonValue | DetachedWork | Promise<JsonValue | DetachedWork>;

export interface MethodDefinition {
  method: string;
  sideEffect: boolean;
  handle(params: JsonValue | undefined, ctx: HandlerContext): HandlerResult;
  /**
   * Extract a client-supplied idempotency key, if any. Only consulted for
   * side-effect methods. Returning null means "execute normally".
   */
  idempotencyKeyOf?(params: JsonValue | undefined): string | null;
  /**
   * Stable fingerprint of the request payload, used to detect the same
   * idempotency key being reused with DIFFERENT parameters.
   */
  fingerprint?(params: JsonValue | undefined): string;
}

/** A typed failure thrown by handlers -> mapped to an RPC error object. */
export class MethodError extends Error {
  constructor(
    readonly code: number,
    message: string,
    readonly data?: JsonValue,
  ) {
    super(message);
    this.name = 'MethodError';
  }
}
