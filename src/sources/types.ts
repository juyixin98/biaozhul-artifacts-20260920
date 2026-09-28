/**
 * Source SPI — every "upstream" the composite can call implements this.
 *
 * Sources are local fixtures or local-only adapters; they never reach a real
 * network in this project.
 */
import type { CompositeError } from '../kernel/errors.js';

/** Per-request context propagated to EVERY source call. */
export interface CallContext {
  /**
   * Snapshot token for the whole composite request. Every source receives the
   * SAME token. Sources that cannot honor snapshot reads set
   * `capabilities.snapshot = false`, and the engine records a consistency
   * limitation instead of pretending the read was snapshot-consistent.
   */
  snapshotToken: string;
  /** Absolute deadline (epoch ms). Sources must stop work after this time. */
  deadline: number;
  /** Remaining budget in ms — convenience for sources. */
  timeoutMs: number;
  /** Aborted once the deadline fires or the composite is cancelled. */
  signal: AbortSignal;
  /** Monotonic run id, also used to correlate log lines. */
  runId: string;
}

export interface SourceCapabilities {
  /** Whether this source can serve a pinned snapshot token. */
  snapshot: boolean;
  /** Source/version tag, e.g. `users-fixture@v2`. Used for version-mismatch detection. */
  version: string;
}

export interface SourceCallResult {
  ok: boolean;
  value?: unknown;
  error?: CompositeError;
}

export interface Source {
  readonly name: string;
  readonly capabilities: SourceCapabilities;
  /**
   * Execute one operation. MUST observe `ctx.signal` / `ctx.deadline`:
   * resolve promptly after abort, never keep unbounded background work alive.
   */
  call(method: string, request: unknown, ctx: CallContext): Promise<unknown>;
  /**
   * Optional per-call consistency report. When omitted, the engine falls back
   * to static `capabilities`. Sources that pin per-token versions return the
   * version actually served, enabling version-mismatch detection.
   */
  consistency?(ctx: CallContext): { snapshotHonored: boolean; dataVersion: string };
}
