/**
 * Contract layer — shared domain model.
 *
 * These types describe the HTTP conditional-request contract (RFC 9110
 * preconditions, entity tags and selected representations) independently of
 * any transport or storage choice.
 */

export type HttpMethod = "GET" | "HEAD" | "PUT" | "DELETE";

export type TransactionMode = "deferred" | "immediate";

/** A selected representation as stored at one committed version. */
export interface Snapshot {
  readonly resourceId: string;
  /** Monotonic, gap-free version number, starting at 1. */
  readonly version: number;
  /** Raw representation bytes (UTF-8). */
  readonly body: string;
  /**
   * Canonical STRONG entity-tag token for this snapshot, including the
   * surrounding double quotes and WITHOUT any "W/" prefix, e.g. `"v3-9f86d081884c"`.
   */
  readonly etag: string;
  readonly updatedAtMs: number;
}

/** Raw conditional header material extracted from a request. */
export interface ConditionInput {
  readonly ifMatch?: string;
  readonly ifNoneMatch?: string;
  readonly ifUnmodifiedSince?: string;
  readonly ifModifiedSince?: string;
  /** Idempotency-Key — enables safe replay after a lost response. */
  readonly idempotencyKey?: string;
}

/** Stable, machine-readable failure categories. Asserted directly by tests. */
export type FailureCategory =
  | "malformed-if-match"
  | "malformed-if-none-match"
  | "malformed-date"
  | "if-match-mismatch"
  | "if-none-match-exists"
  | "if-unmodified-since-modified"
  | "idempotency-replay-conflict"
  | "write-lock-busy"
  | "not-found";

/**
 * One traceable evaluation step. The decision log and structured diagnostics
 * expose these so every verdict can be reconstructed from the inputs.
 */
export type EvalStep =
  | {
      stage: "if-match" | "if-none-match";
      comparison: "strong" | "weak";
      observed: string | null;
      candidates: ReadonlyArray<{ raw: string; weak: boolean; matched: boolean; note?: string }>;
      star: boolean;
      result: "match" | "no-match";
    }
  | {
      stage: "if-unmodified-since" | "if-modified-since";
      suppliedDate: string;
      parsedMs: number | null;
      observedUpdatedAtMs: number | null;
      result: "unmodified" | "modified" | "ignored-malformed" | "ignored-because-if-none-match" | "ignored-method";
    };

export type VerdictName = "proceed" | "not-modified" | "precondition-failed" | "malformed";

/** Result of pure conditional-header evaluation against a current snapshot. */
export type PreconditionVerdict =
  | { verdict: "proceed"; steps: EvalStep[] }
  | { verdict: "not-modified"; steps: EvalStep[] }
  | { verdict: "precondition-failed"; category: FailureCategory; detail: string; steps: EvalStep[] }
  | { verdict: "malformed"; category: FailureCategory; detail: string; steps: EvalStep[] };

export interface CorrelationIds {
  readonly runId: string;
  readonly clientId: string;
  readonly requestId: string;
}

/** Persisted/observable record of one conditional operation. */
export interface DecisionRecord {
  readonly runId: string;
  readonly clientId: string;
  readonly requestId: string;
  readonly method: HttpMethod;
  readonly resourceId: string;
  readonly requestBodyHash: string | null;
  readonly conditions: ConditionInput;
  readonly observedVersion: number | null;
  readonly observedEtag: string | null;
  readonly observedUpdatedAtMs: number | null;
  readonly steps: EvalStep[];
  readonly verdict: VerdictName;
  readonly failureCategory: FailureCategory | null;
  readonly outcomeStatus: number;
  readonly resultingVersion: number | null;
  readonly resultingEtag: string | null;
  readonly replayed: boolean;
  readonly atMs: number;
}
