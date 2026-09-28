/**
 * State-adapter layer — storage port.
 *
 * The execution kernel depends only on this interface, never on SQLite
 * directly. Every conditional evaluation and its write happen inside one
 * `run("immediate", ...)` callback, i.e. one database transaction.
 */

import type { DecisionRecord } from "../contract/model.js";

export interface StoredResource {
  readonly resourceId: string;
  readonly version: number;
  readonly body: string;
  readonly etag: string;
  readonly contentType: string;
  readonly updatedAtMs: number;
}

export interface IdempotentOutcome {
  readonly resourceId: string;
  readonly method: string;
  readonly requestBodyHash: string;
  readonly status: number;
  readonly responseBody: string | null;
  readonly responseEtag: string | null;
  readonly contentType: string | null;
  readonly resultingVersion: number | null;
  readonly atMs: number;
}

export interface NewVersionInput {
  readonly resourceId: string;
  readonly version: number;
  readonly body: string;
  readonly etag: string;
  readonly contentType: string;
  readonly updatedAtMs: number;
}

export interface StoreTransaction {
  getCurrent(resourceId: string): StoredResource | null;
  /** Next version after all retained history (1 for a never-seen id). */
  nextVersionFromHistory(resourceId: string): number;
  upsertCurrent(input: NewVersionInput): void;
  insertHistory(input: NewVersionInput): void;
  deleteCurrent(resourceId: string): void;

  getIdempotentOutcome(key: string): IdempotentOutcome | null;
  putIdempotentOutcome(key: string, outcome: IdempotentOutcome): void;

  insertDecision(record: DecisionRecord): void;
}

export interface ResourceStore {
  readonly kind: string;
  /** Run `fn` inside one transaction. Writers MUST use "immediate". */
  run<T>(mode: "deferred" | "immediate", fn: (tx: StoreTransaction) => T): T;
  /** Autocommit read AFTER commit: proves response data came from the committed snapshot. */
  readCommitted<T>(fn: (tx: StoreTransaction) => T): T;
  listDecisions(filter: { runId?: string; clientId?: string; resourceId?: string }): DecisionRecord[];
  listHistory(resourceId: string): Array<{ version: number; etag: string; updatedAtMs: number }>;
  close(): void;
}

export class StorageError extends Error {
  override readonly name = "StorageError";
  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
  }
}
