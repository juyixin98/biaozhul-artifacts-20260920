/**
 * Execution kernel.
 *
 * One public operation per HTTP method. Each operation runs condition
 * evaluation, the optional write, the idempotency record and the decision
 * log entry inside a SINGLE store transaction. Response payloads are then
 * re-read from the store AFTER commit (readCommitted), proving the body and
 * ETag sent to the client come from the committed snapshot.
 *
 * The kernel has no Fastify/HTTP types: the transport adapter maps these
 * outcomes to status codes and headers.
 */

import { buildStrongETag, renderETag, sha256Hex } from "../contract/etag.js";
import { evaluatePreconditions, type CurrentRepresentation } from "../contract/preconditions.js";
import type {
  ConditionInput,
  CorrelationIds,
  DecisionRecord,
  FailureCategory,
  HttpMethod,
} from "../contract/model.js";
import {
  StorageError,
  type IdempotentOutcome,
  type ResourceStore,
  type StoredResource,
  type StoreTransaction,
} from "../state/store.js";

export type ETagStrength = "strong" | "weak";

export interface KernelDeps {
  readonly store: ResourceStore;
  readonly etagStrength: ETagStrength;
  /** Injected for deterministic fixtures; defaults to Date.now. */
  readonly clock: () => number;
}

export interface CommittedSnapshot {
  readonly resourceId: string;
  readonly version: number;
  readonly body: string;
  readonly canonicalEtag: string;
  /** Wire form honoring the configured emission strength. */
  readonly etagHeader: string;
  readonly contentType: string;
  readonly updatedAtMs: number;
}

export type KernelOutcome =
  | { kind: "present"; status: 200 | 201; snapshot: CommittedSnapshot; replayed: boolean; record: DecisionRecord }
  | { kind: "no-content"; status: 204; replayed: boolean; record: DecisionRecord }
  | { kind: "not-modified"; status: 304; snapshot: CommittedSnapshot; record: DecisionRecord }
  | { kind: "not-found"; status: 404; category: "not-found"; record: DecisionRecord }
  | {
      kind: "precondition-failed";
      status: 412;
      category: FailureCategory;
      detail: string;
      record: DecisionRecord;
    }
  | { kind: "malformed"; status: 400; category: FailureCategory; detail: string; record: DecisionRecord }
  | {
      kind: "replay-conflict";
      status: 409;
      category: "idempotency-replay-conflict";
      detail: string;
      record: DecisionRecord;
    }
  | { kind: "busy"; status: 503; category: "write-lock-busy"; detail: string; record: DecisionRecord };

export interface KernelRequest {
  readonly resourceId: string;
  readonly conditions: ConditionInput;
  readonly correlation: CorrelationIds;
  readonly body?: string;
  readonly contentType?: string;
}

/** Internal transaction result describing a recognized idempotency replay. */
type ReplayResolution = {
  record: DecisionRecord;
  result:
    | { mode: "replay"; outcome: KernelOutcome }
    | { mode: "conflict"; detail: string; record: DecisionRecord };
};

function toCurrent(row: StoredResource | null): CurrentRepresentation {
  return {
    exists: row !== null,
    strongEtag: row?.etag ?? null,
    version: row?.version ?? null,
    updatedAtMs: row?.updatedAtMs ?? null,
  };
}

function snapshotOf(row: StoredResource, strength: ETagStrength): CommittedSnapshot {
  return {
    resourceId: row.resourceId,
    version: row.version,
    body: row.body,
    canonicalEtag: row.etag,
    etagHeader: renderETag(row.etag, strength),
    contentType: row.contentType,
    updatedAtMs: row.updatedAtMs,
  };
}

function baseRecord(
  req: KernelRequest,
  method: HttpMethod,
  bodyHash: string | null,
  atMs: number,
): DecisionRecord {
  return {
    runId: req.correlation.runId,
    clientId: req.correlation.clientId,
    requestId: req.correlation.requestId,
    method,
    resourceId: req.resourceId,
    requestBodyHash: bodyHash,
    conditions: req.conditions,
    observedVersion: null,
    observedEtag: null,
    observedUpdatedAtMs: null,
    steps: [],
    verdict: "proceed",
    failureCategory: null,
    outcomeStatus: 0,
    resultingVersion: null,
    resultingEtag: null,
    replayed: false,
    atMs,
  };
}

export class Kernel {
  private readonly store: ResourceStore;
  private readonly strength: ETagStrength;
  private readonly clock: () => number;

  constructor(deps: KernelDeps) {
    this.store = deps.store;
    this.strength = deps.etagStrength;
    this.clock = deps.clock;
  }

  // ---- GET / HEAD -------------------------------------------------------

  get(req: KernelRequest): KernelOutcome {
    const atMs = this.clock();
    const bodyHash = null;
    try {
      const txResult = this.store.run("deferred", (tx) => {
        const current = tx.getCurrent(req.resourceId);
        const verdict = evaluatePreconditions("GET", req.conditions, toCurrent(current));
        let record: DecisionRecord = { ...baseRecord(req, "GET", bodyHash, atMs) };
        if (current) {
          record = {
            ...record,
            observedVersion: current.version,
            observedEtag: current.etag,
            observedUpdatedAtMs: current.updatedAtMs,
          };
        }
        record = { ...record, steps: verdict.steps, verdict: verdict.verdict };

        // Conditional failures take precedence over existence: If-Match on
        // an absent representation is 412, not 404 (RFC 9110 13.1.1).
        if (verdict.verdict === "precondition-failed" || verdict.verdict === "malformed") {
          const status = verdict.verdict === "malformed" ? 400 : 412;
          record = {
            ...record,
            failureCategory: verdict.category,
            outcomeStatus: status,
          };
          tx.insertDecision(record);
          return {
            mode: "reject" as const,
            status,
            category: verdict.category,
            detail: verdict.detail,
            record,
          };
        }
        if (!current) {
          record = {
            ...record,
            verdict: "precondition-failed",
            failureCategory: "not-found",
            outcomeStatus: 404,
          };
          tx.insertDecision(record);
          return { mode: "not-found" as const, record };
        }
        if (verdict.verdict === "not-modified") {
          record = { ...record, outcomeStatus: 304 };
          tx.insertDecision(record);
          return { mode: "not-modified" as const, row: current, record };
        }
        record = { ...record, outcomeStatus: 200 };
        tx.insertDecision(record);
        return { mode: "present" as const, row: current, record };
      });

      if (txResult.mode === "not-found") {
        return { kind: "not-found", status: 404, category: "not-found", record: txResult.record };
      }
      if (txResult.mode === "reject") {
        return txResult.status === 400
          ? { kind: "malformed", status: 400, category: txResult.category, detail: txResult.detail, record: txResult.record }
          : { kind: "precondition-failed", status: 412, category: txResult.category, detail: txResult.detail, record: txResult.record };
      }

      // Re-read AFTER commit: the response body/ETag are the committed snapshot.
      const committed = this.store.readCommitted((tx) => tx.getCurrent(req.resourceId));
      if (!committed) {
        throw new Error("Committed read after GET returned no row (internal inconsistency)");
      }
      const snapshot = snapshotOf(committed, this.strength);

      switch (txResult.mode) {
        case "not-modified":
          return { kind: "not-modified", status: 304, snapshot, record: txResult.record };
        case "present":
          return { kind: "present", status: 200, snapshot, replayed: false, record: txResult.record };
      }
    } catch (err) {
      return this.mapStorageError(err, req, "GET", bodyHash);
    }
  }

  // ---- PUT --------------------------------------------------------------

  put(req: KernelRequest): KernelOutcome {
    const body = req.body ?? "";
    const contentType = req.contentType ?? "application/octet-stream";
    const bodyHash = sha256Hex(body);
    const atMs = this.clock();

    try {
      const result = this.store.run("immediate", (tx) => {
        let record = { ...baseRecord(req, "PUT", bodyHash, atMs) };

        // Idempotency replay handling participates in the same transaction.
        const replay = this.resolveReplay(tx, req, "PUT", bodyHash, record);
        if (replay) {
          tx.insertDecision(replay.record);
          return replay.result;
        }

        const current = tx.getCurrent(req.resourceId);
        if (current) {
          record = {
            ...record,
            observedVersion: current.version,
            observedEtag: current.etag,
            observedUpdatedAtMs: current.updatedAtMs,
          };
        }
        const verdict = evaluatePreconditions("PUT", req.conditions, toCurrent(current));
        record = { ...record, steps: verdict.steps };

        if (verdict.verdict === "not-modified") {
          // Unreachable for PUT: If-None-Match on an unsafe method yields
          // precondition-failed, never not-modified.
          throw new Error("Internal error: not-modified verdict for PUT");
        }
        if (verdict.verdict === "malformed" || verdict.verdict === "precondition-failed") {
          const status = verdict.verdict === "malformed" ? 400 : 412;
          record = {
            ...record,
            verdict: verdict.verdict,
            failureCategory: verdict.category,
            outcomeStatus: status,
          };
          tx.insertDecision(record);
          return { mode: "reject" as const, status, category: verdict.category, detail: verdict.detail, record };
        }

        // Proceed: create the next version. Version numbers are gap-free per
        // resource even across delete/recreate (driven by history).
        const version = current ? current.version + 1 : tx.nextVersionFromHistory(req.resourceId);
        const etag = buildStrongETag(version, body);
        const updatedAtMs = this.clock();
        const write = { resourceId: req.resourceId, version, body, etag, contentType, updatedAtMs };
        tx.upsertCurrent(write);
        tx.insertHistory(write);

        const status = current ? 200 : 201;
        record = {
          ...record,
          verdict: "proceed",
          outcomeStatus: status,
          resultingVersion: version,
          resultingEtag: etag,
        };
        if (req.conditions.idempotencyKey) {
          tx.putIdempotentOutcome(req.conditions.idempotencyKey, {
            resourceId: req.resourceId,
            method: "PUT",
            requestBodyHash: bodyHash,
            status,
            responseBody: body,
            responseEtag: etag,
            contentType,
            resultingVersion: version,
            atMs: updatedAtMs,
          });
        }
        tx.insertDecision(record);
        return { mode: "written" as const, status, record };
      });

      if (result.mode === "reject") {
        return result.status === 400
          ? { kind: "malformed", status: 400, category: result.category, detail: result.detail, record: result.record }
          : { kind: "precondition-failed", status: 412, category: result.category, detail: result.detail, record: result.record };
      }
      if (result.mode === "conflict") {
        return {
          kind: "replay-conflict",
          status: 409,
          category: "idempotency-replay-conflict",
          detail: result.detail,
          record: result.record,
        };
      }
      if (result.mode === "replay") {
        return result.outcome;
      }

      const committed = this.store.readCommitted((tx) => tx.getCurrent(req.resourceId));
      if (!committed) throw new Error("Committed read after PUT returned no row");
      if (committed.version !== result.record.resultingVersion || committed.etag !== result.record.resultingEtag) {
        throw new Error("Committed snapshot diverges from write record (internal inconsistency)");
      }
      return {
        kind: "present",
        status: result.status === 201 ? 201 : 200,
        snapshot: snapshotOf(committed, this.strength),
        replayed: false,
        record: result.record,
      };
    } catch (err) {
      return this.mapStorageError(err, req, "PUT", bodyHash);
    }
  }

  // ---- DELETE -----------------------------------------------------------

  delete(req: KernelRequest): KernelOutcome {
    const bodyHash = null;
    const atMs = this.clock();
    try {
      const result = this.store.run("immediate", (tx) => {
        let record = { ...baseRecord(req, "DELETE", bodyHash, atMs) };

        const replay = this.resolveReplay(tx, req, "DELETE", "", record);
        if (replay) {
          tx.insertDecision(replay.record);
          return replay.result;
        }

        const current = tx.getCurrent(req.resourceId);
        if (current) {
          record = {
            ...record,
            observedVersion: current.version,
            observedEtag: current.etag,
            observedUpdatedAtMs: current.updatedAtMs,
          };
        }
        const verdict = evaluatePreconditions("DELETE", req.conditions, toCurrent(current));
        record = { ...record, steps: verdict.steps };

        // Conditional failures take precedence over existence (If-Match on
        // an absent representation is 412, not 404).
        if (verdict.verdict === "precondition-failed" || verdict.verdict === "malformed") {
          const status = verdict.verdict === "malformed" ? 400 : 412;
          record = {
            ...record,
            verdict: verdict.verdict,
            failureCategory: verdict.category,
            outcomeStatus: status,
          };
          tx.insertDecision(record);
          return {
            mode: "reject" as const,
            status,
            category: verdict.category,
            detail: verdict.detail,
            record,
          };
        }
        if (verdict.verdict === "not-modified") {
          // Unreachable for DELETE: not-modified is only returned for safe
          // methods. Fail loudly rather than mapping an unknown state to a
          // generic success/error.
          throw new Error("Internal error: not-modified verdict for DELETE");
        }

        if (!current) {
          record = {
            ...record,
            verdict: "precondition-failed",
            failureCategory: "not-found",
            outcomeStatus: 404,
          };
          tx.insertDecision(record);
          return { mode: "not-found" as const, record };
        }

        tx.deleteCurrent(req.resourceId);
        record = {
          ...record,
          verdict: "proceed",
          outcomeStatus: 204,
          resultingVersion: current.version,
          resultingEtag: current.etag,
        };
        if (req.conditions.idempotencyKey) {
          tx.putIdempotentOutcome(req.conditions.idempotencyKey, {
            resourceId: req.resourceId,
            method: "DELETE",
            requestBodyHash: "",
            status: 204,
            responseBody: null,
            responseEtag: current.etag,
            contentType: null,
            resultingVersion: current.version,
            atMs: this.clock(),
          });
        }
        tx.insertDecision(record);
        return { mode: "deleted" as const, record };
      });

      switch (result.mode) {
        case "not-found":
          return { kind: "not-found", status: 404, category: "not-found", record: result.record };
        case "reject":
          return result.status === 400
            ? { kind: "malformed", status: 400, category: result.category, detail: result.detail, record: result.record }
            : { kind: "precondition-failed", status: 412, category: result.category, detail: result.detail, record: result.record };
        case "conflict":
          return {
            kind: "replay-conflict",
            status: 409,
            category: "idempotency-replay-conflict",
            detail: result.detail,
            record: result.record,
          };
        case "replay":
          return result.outcome;
        case "deleted": {
          const committed = this.store.readCommitted((tx) => tx.getCurrent(req.resourceId));
          if (committed) throw new Error("Committed read after DELETE still shows the resource");
          return { kind: "no-content", status: 204, replayed: false, record: result.record };
        }
      }
    } catch (err) {
      return this.mapStorageError(err, req, "DELETE", bodyHash);
    }
  }

  // ---- helpers ----------------------------------------------------------

  /**
   * Returns a replay descriptor when an Idempotency-Key has been seen:
   * - same request body hash  -> stored outcome is replayed verbatim
   * - different request body  -> 409 idempotency-replay-conflict
   */
  private resolveReplay(
    tx: StoreTransaction,
    req: KernelRequest,
    method: HttpMethod,
    bodyHash: string,
    record: DecisionRecord,
  ): ReplayResolution | null {
    const key = req.conditions.idempotencyKey;
    if (!key) return null;
    const stored: IdempotentOutcome | null = tx.getIdempotentOutcome(key);
    if (!stored) return null;

    // An idempotency key identifies one specific request: reusing it for a
    // different resource/method OR a different body is a client error, never
    // a silent cross-resource replay.
    const scopeMismatch =
      stored.resourceId !== req.resourceId || stored.method !== method;
    if (scopeMismatch || stored.requestBodyHash !== bodyHash) {
      const reason = scopeMismatch
        ? `key is bound to ${stored.method} ${stored.resourceId}, reused for ${method} ${req.resourceId}`
        : `stored sha-256 ${stored.requestBodyHash.slice(0, 12)}…, got ${bodyHash.slice(0, 12)}…`;
      const conflictRecord: DecisionRecord = {
        ...record,
        observedVersion: null,
        verdict: "precondition-failed",
        failureCategory: "idempotency-replay-conflict",
        outcomeStatus: 409,
        replayed: true,
        resultingVersion: stored.resultingVersion,
        resultingEtag: stored.responseEtag,
      };
      return {
        record: conflictRecord,
        result: {
          mode: "conflict",
          detail: `Idempotency-Key ${JSON.stringify(key)} was already used for a different request (${reason})`,
          record: conflictRecord,
        },
      };
    }

    // Faithful replay: rebuild the response from the stored snapshot.
    const replayRecord: DecisionRecord = {
      ...record,
      observedVersion: stored.resultingVersion,
      observedEtag: stored.responseEtag,
      verdict: "proceed",
      outcomeStatus: stored.status,
      resultingVersion: stored.resultingVersion,
      resultingEtag: stored.responseEtag,
      replayed: true,
    };

    if (stored.status === 204 || stored.method === "DELETE") {
      return {
        record: replayRecord,
        result: { mode: "replay", outcome: { kind: "no-content", status: 204, replayed: true, record: replayRecord } },
      };
    }
    const current = tx.getCurrent(req.resourceId);
    // Prefer the live row when it is still exactly the stored version;
    // otherwise use the stored response bytes verbatim (response was lost,
    // state may legitimately have moved on).
    const row: StoredResource | null =
      current && current.version === stored.resultingVersion ? current : null;
    const snapshot: CommittedSnapshot = row
      ? snapshotOf(row, this.strength)
      : {
          resourceId: stored.resourceId,
          version: stored.resultingVersion ?? 0,
          body: stored.responseBody ?? "",
          canonicalEtag: stored.responseEtag ?? "",
          etagHeader: renderETag(stored.responseEtag ?? "", this.strength),
          contentType: stored.contentType ?? "application/octet-stream",
          updatedAtMs: stored.atMs,
        };
    return {
      record: replayRecord,
      result: {
        mode: "replay",
        outcome: {
          kind: "present",
          status: stored.status === 201 ? 201 : 200,
          snapshot,
          replayed: true,
          record: replayRecord,
        },
      },
    };
  }

  private mapStorageError(
    err: unknown,
    req: KernelRequest,
    method: HttpMethod,
    bodyHash: string | null,
  ): KernelOutcome {
    if (err instanceof StorageError && err.code === "write-lock-busy") {
      const record: DecisionRecord = {
        ...baseRecord(req, method, bodyHash, this.clock()),
        verdict: "precondition-failed",
        failureCategory: "write-lock-busy",
        outcomeStatus: 503,
      };
      // Record the busy outcome in a separate (short) transaction so the
      // diagnostic trail is complete even when the write tx could not start.
      try {
        this.store.run("immediate", (tx) => tx.insertDecision(record));
      } catch {
        // diagnostics must not mask the original 503
      }
      return {
        kind: "busy",
        status: 503,
        category: "write-lock-busy",
        detail: err.message,
        record,
      };
    }
    throw err;
  }
}
