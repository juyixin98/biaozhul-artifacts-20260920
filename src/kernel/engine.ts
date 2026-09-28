/**
 * Execution kernel — orchestration only.
 *
 * It owns request-level ledger rows, schedules batch entries concurrently,
 * assembles the transport result (single/batch/204/error/interrupted), and
 * handles duplicate-id diagnosis. Per-call execution, operation numbers and
 * idempotency live in call-executor.ts; shared primitives in support.ts.
 */

import { INVALID_REQUEST, STABLE_MESSAGES } from "../protocol/errors.js";
import type { FailureCategory, RpcResponse } from "../protocol/types.js";
import { isNotification } from "../protocol/types.js";
import type { BatchEntry, ParseOutcome } from "../protocol/parser.js";
import { noopLogger } from "../diagnostics/logger.js";
import type {
  CallStatus,
  LedgerStore,
  PayloadKind,
} from "../state/store.js";
import { FixtureAccounts, FixtureSecretVault } from "./fixtures.js";
import { encodeRpcId, newRequestCorr } from "./ids.js";
import { buildMethodRegistry, type MethodDefinition } from "./methods.js";
import { runInvalidEntry, runValidEntry } from "./call-executor.js";
import { safeParseId } from "./support.js";
import type {
  KernelDeps,
  KernelMeta,
  KernelRuntime,
  KernelTransportResult,
  SettledCall,
} from "./types.js";
import type { DiagnosticsLogger } from "../diagnostics/logger.js";

const MAX_BATCH = 100;

export class Kernel implements KernelRuntime {
  readonly store: LedgerStore;
  readonly logger: DiagnosticsLogger;
  readonly registry: Map<string, MethodDefinition>;
  readonly accounts: FixtureAccounts;
  readonly vault: FixtureSecretVault;
  readonly clock: () => Date;
  readonly idemLocks = new Map<string, Promise<void>>();
  readonly background = new Set<Promise<void>>();

  constructor(deps: KernelDeps) {
    this.store = deps.store;
    this.logger = deps.logger ?? noopLogger;
    this.registry = deps.registry ?? buildMethodRegistry();
    this.accounts = deps.accounts ?? new FixtureAccounts();
    this.vault = deps.vault ?? new FixtureSecretVault();
    this.clock = deps.clock ?? (() => new Date());
  }

  getAccounts(): FixtureAccounts {
    return this.accounts;
  }

  trackBackground(p: Promise<void>): void {
    this.background.add(p);
    void p.finally(() => this.background.delete(p));
  }

  /** Resolves once every detached async continuation has settled. */
  async awaitBackgroundSettled(): Promise<void> {
    while (this.background.size > 0) {
      await Promise.allSettled([...this.background]);
    }
  }

  /** Compose a parsed payload into a transport result. */
  async handle(
    outcome: ParseOutcome,
    meta: KernelMeta,
  ): Promise<KernelTransportResult> {
    if (outcome.kind === "parseError") {
      return this.recordTopLevelFailure({
        payloadKind: "parse_error",
        code: outcome.code,
        category: outcome.category,
        message: outcome.message,
        reason: outcome.reason,
        layer: "parse",
        httpStatus: 500,
        meta,
      });
    }
    if (outcome.kind === "emptyBatch" || outcome.kind === "invalidEnvelope") {
      return this.recordTopLevelFailure({
        payloadKind: outcome.kind === "emptyBatch" ? "empty_batch" : "single",
        code: outcome.code,
        category: outcome.category,
        message: outcome.message,
        reason: outcome.reason,
        layer: "contract",
        httpStatus: 400,
        meta,
      });
    }

    const requestCorr = newRequestCorr();
    const payloadKind: PayloadKind =
      outcome.kind === "single" ? "single" : "batch";
    this.store.beginRequest({
      requestCorr,
      receivedAt: this.clock().toISOString(),
      payloadKind,
      sizeBytes: meta.sizeBytes,
    });

    const entries: readonly BatchEntry[] =
      outcome.kind === "single" ? [outcome.entry] : outcome.entries;

    if (entries.length > MAX_BATCH) {
      return this.recordTopLevelFailure({
        payloadKind,
        code: INVALID_REQUEST,
        category: "invalid_request",
        message: STABLE_MESSAGES.invalidRequest,
        reason: `batch size ${entries.length} exceeds maximum ${MAX_BATCH}`,
        layer: "contract",
        httpStatus: 400,
        meta,
      });
    }

    // Concurrent execution; attribution is by position + callCorr only.
    const settled = await Promise.all(
      entries.map((entry) =>
        entry.kind === "invalid"
          ? runInvalidEntry(this, requestCorr, entry)
          : runValidEntry(this, requestCorr, entry, meta.signal),
      ),
    );

    // Duplicate RPC ids are legal ("clients should not expect uniqueness");
    // every entry already got its own response. Log for client-bug diagnosis.
    this.warnIfDuplicateIds(requestCorr, entries);

    const interrupted = meta.signal?.aborted ?? false;
    let pendingMarks = { calls: 0, operations: 0 };
    if (interrupted) {
      pendingMarks = this.store.interruptPending(
        requestCorr,
        this.clock().toISOString(),
      );
    }

    const notificationCount = entries.reduce(
      (n, e) =>
        n + (e.kind === "message" && isNotification(e.request) ? 1 : 0),
      0,
    );
    const invalidCount = entries.filter((e) => e.kind === "invalid").length;

    this.store.finishRequest({
      requestCorr,
      finishedAt: this.clock().toISOString(),
      callCount: entries.length,
      notificationCount,
      invalidCount,
      verdict: interrupted ? "undecidable" : "accepted",
      layer: "execution",
      failureCategory: interrupted ? "interrupted" : null,
      reason: interrupted
        ? `connection lost before response delivery; marked ${pendingMarks.calls} call(s) and ${pendingMarks.operations} operation(s) interrupted`
        : null,
    });

    if (interrupted) {
      this.logger.undecidable({
        requestCorr,
        decision: "undecidable:transport_aborted",
        detail: pendingMarks,
      });
      return { kind: "interrupted" };
    }

    if (outcome.kind === "single") {
      const only = settled[0]!;
      if (only.response === null) {
        return { kind: "notificationOnly", httpStatus: 204 };
      }
      const httpStatus =
        "error" in only.response &&
        only.response.error.data?.category === "invalid_request"
          ? 400
          : 200;
      return { kind: "single", httpStatus, response: only.response };
    }

    const responses = settled
      .filter((s): s is SettledCall & { response: RpcResponse } =>
        s.response !== null,
      )
      .sort((a, b) => a.position - b.position)
      .map((s) => s.response);

    if (responses.length === 0) {
      return { kind: "notificationOnly", httpStatus: 204 };
    }
    return { kind: "batch", httpStatus: 200, responses };
  }

  private recordTopLevelFailure(input: {
    payloadKind: PayloadKind;
    code: number;
    category: FailureCategory;
    message: string;
    reason: string;
    layer: "parse" | "contract";
    httpStatus: 400 | 500;
    meta: KernelMeta;
  }): KernelTransportResult {
    const requestCorr = newRequestCorr();
    const now = this.clock().toISOString();
    this.store.beginRequest({
      requestCorr,
      receivedAt: now,
      payloadKind: input.payloadKind,
      sizeBytes: input.meta.sizeBytes,
    });
    this.store.finishRequest({
      requestCorr,
      finishedAt: now,
      callCount: 0,
      notificationCount: 0,
      invalidCount: 0,
      verdict: "rejected",
      layer: input.layer,
      failureCategory: input.category,
      reason: input.reason,
    });
    this.logger.rejected({
      requestCorr,
      decision: `rejected:${input.layer}`,
      category: input.category,
      detail: { reason: input.reason, sizeBytes: input.meta.sizeBytes },
    });
    return {
      kind: "topLevelError",
      httpStatus: input.httpStatus,
      response: {
        jsonrpc: "2.0",
        error: {
          code: input.code,
          message: input.message,
          data: { category: input.category, correlationId: requestCorr },
        },
        id: null,
      },
    };
  }

  private warnIfDuplicateIds(
    requestCorr: string,
    entries: readonly BatchEntry[],
  ): void {
    const seen = new Map<string, number[]>();
    for (const entry of entries) {
      if (entry.kind !== "message" || isNotification(entry.request)) continue;
      const key = encodeRpcId(entry.request.id);
      if (key === null) continue;
      const positions = seen.get(key) ?? [];
      positions.push(entry.position);
      seen.set(key, positions);
    }
    for (const [rpcIdJson, positions] of seen) {
      if (positions.length > 1) {
        this.logger.warn({
          requestCorr,
          decision: "duplicate_rpc_id_accepted",
          detail: {
            rpcId: safeParseId(rpcIdJson),
            positions,
            policy: "each position answered independently; no cross-talk",
          },
        });
      }
    }
  }
}

export type { CallStatus };
