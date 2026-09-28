/**
 * Typed result envelope emitted by the execution kernel.
 */
import type { ErrorDetail } from './errors.js';
import type { FieldStatus, FieldRequirement } from '../contract/types.js';

export type RunStatus = 'complete' | 'partial' | 'failed';

/** Execution state of a single dependency-graph node. */
export type NodeState =
  | 'pending'
  | 'running'
  | 'resolved'
  | 'failed'
  | 'timeout'
  | 'cancelled'
  | 'skipped';

export interface NodeExecution {
  callId: string;
  source: string;
  method: string;
  requirement: FieldRequirement | 'internal';
  state: NodeState;
  startedAt: number | null;
  endedAt: number | null;
  durationMs: number | null;
  /** Exactly one source invocation per resolved/failed node — asserted in tests. */
  attempts: number;
  dependencyIds: string[];
  /** Whether this source honored the request snapshot token. */
  snapshotHonored: boolean;
  sourceVersion: string;
  value?: unknown;
  error?: ErrorDetail;
}

export interface ConsistencyLimitation {
  type: 'SNAPSHOT_UNSUPPORTED' | 'VERSION_MISMATCH';
  sources: string[];
  detail: string;
  versions?: Record<string, string>;
}

export interface CompositeResult {
  runId: string;
  contract: string;
  status: RunStatus;
  snapshotToken: string;
  startedAt: number;
  endedAt: number;
  deadlineMs: number;
  /** Typed response payload; failed required fields are absent. */
  data: Record<string, unknown>;
  fields: FieldStatus[];
  nodes: NodeExecution[];
  limitations: ConsistencyLimitation[];
  /** Populated when status === 'failed'. */
  errors: ErrorDetail[];
}
