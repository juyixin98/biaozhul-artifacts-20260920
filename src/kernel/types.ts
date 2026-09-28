/**
 * Shared kernel types.
 */

import type { RpcErrorResponse, RpcResponse } from "../protocol/types.js";
import type { DiagnosticsLogger } from "../diagnostics/logger.js";
import type { LedgerStore } from "../state/store.js";
import type { FixtureAccounts, FixtureSecretVault } from "./fixtures.js";
import type { MethodDefinition } from "./methods.js";

export type KernelTransportResult =
  | { kind: "single"; httpStatus: 200 | 400; response: RpcResponse }
  | { kind: "batch"; httpStatus: 200; responses: readonly RpcResponse[] }
  | { kind: "notificationOnly"; httpStatus: 204 }
  | {
      kind: "topLevelError";
      httpStatus: 400 | 500;
      response: RpcErrorResponse;
    }
  | { kind: "interrupted" };

export interface KernelMeta {
  readonly signal?: AbortSignal;
  readonly sizeBytes: number;
}

export interface KernelDeps {
  readonly store: LedgerStore;
  readonly logger?: DiagnosticsLogger;
  readonly registry?: Map<string, MethodDefinition>;
  readonly accounts?: FixtureAccounts;
  readonly vault?: FixtureSecretVault;
  readonly clock?: () => Date;
}

export interface SettledCall {
  readonly position: number;
  /** null exactly for notifications and for work interrupted mid-flight. */
  readonly response: RpcResponse | null;
}

/** Mutable, process-wide coordination state shared with the call executor. */
export interface KernelRuntime {
  readonly store: LedgerStore;
  readonly logger: DiagnosticsLogger;
  readonly registry: Map<string, MethodDefinition>;
  readonly accounts: FixtureAccounts;
  readonly vault: FixtureSecretVault;
  readonly clock: () => Date;
  /** Per (kind,idempotencyKey) in-flight serialization. */
  readonly idemLocks: Map<string, Promise<void>>;
  /** Detached background continuations (async methods). */
  readonly background: Set<Promise<void>>;
  trackBackground(p: Promise<void>): void;
}
