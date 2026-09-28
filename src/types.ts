import type { FailureDetail } from './errors.js';

/**
 * ============================================================================
 * Composite contract (declarative, parsed by src/contract/)
 * ============================================================================
 */

/** Where an output field gets its value from. */
export type FieldSourceKind =
  | 'source' // value = source lookup result (whole record or a source property)
  | 'constant' // declared static value
  | 'derived'; // computed by an assembler expression from other fields

/** Availability of a dependency / field. */
export type Necessity = 'required' | 'optional';

export interface SourceFieldRef {
  kind: 'source';
  /** Node whose result provides the value. */
  fromNode: string;
  /** Property of the source record. If omitted, the whole record is used. */
  property?: string;
  /** Fallback value used when the dependency is optional and unavailable. */
  fallback?: unknown;
}

export interface ConstantFieldRef {
  kind: 'constant';
  value: unknown;
}

export type DerivedExpression =
  | { op: 'concat'; fields: string[]; separator?: string }
  | { op: 'add'; fields: string[] };

export interface DerivedFieldRef {
  kind: 'der';
  expression: DerivedExpression;
  /** Fields missing from data make a derived field unavailable unless optional. */
  optional?: boolean;
}

export type FieldRef = SourceFieldRef | ConstantFieldRef | DerivedFieldRef;

export interface FieldDeclaration {
  /** Output field path, e.g. "summary.lineItems[].price" — must be unique. */
  path: string;
  required: boolean;
  ref: FieldRef;
}

/** A node in the dependency graph: one call to one data source. */
export interface NodeDeclaration {
  id: string;
  source: string;
  /**
   * Inputs for the source call. A literal value or a reference to an upstream
   * node result (`{ "fromNode": "...", "property": "..." }`).
   */
  params: Record<string, ParamValue>;
  required: Necessity;
  /**
   * Timeout budget override for this node in ms. The effective deadline is
   * min(request deadline, remaining parent budget) — never more.
   */
  timeoutMs?: number;
  /**
   * Snapshot requirement for this node:
   * - 'exact'  : source MUST honour the snapshot token, else STATE_CONFLICT
   * - 'best-effort' (default): if unsupported, annotate CONSISTENCY_LIMITED
   * - 'any'    : snapshot not needed for this node
   */
  snapshotRequirement?: 'exact' | 'best-effort' | 'any';
}

export type ParamValue =
  | { kind: 'literal'; value: unknown }
  | {
      kind: 'ref';
      fromNode: string;
      property?: string;
      /** Optional param: an unavailable upstream yields `default` instead of blocking. */
      optional?: boolean;
      default?: unknown;
    }
  // Resolved from CompositeRequest.params (the real request input).
  | { kind: 'request'; key: string };

export interface CompositeContract {
  name: string;
  version: number;
  /** Nodes keyed by id for lookup; declaration order is preserved. */
  nodes: NodeDeclaration[];
  fields: FieldDeclaration[];
}

/**
 * ============================================================================
 * Source adapter interface (src/sources/)
 * ============================================================================
 */

export interface SourceRequest {
  params: Record<string, unknown>;
  /** Snapshot token propagated from the composite request. */
  snapshotToken: string | null;
  /** Absolute epoch-ms deadline propagated along the dependency graph. */
  deadline: number;
  /** AbortSignal that fires when the run deadline passes / run is cancelled. */
  signal: AbortSignal;
}

export interface SourceMeta {
  /** Whether this source can honour snapshot tokens. */
  supportsSnapshot: boolean;
  /** Logical data version served; mismatch between sources is detectable. */
  dataVersion: string;
}

export interface SourceResult {
  record: Record<string, unknown>;
  meta: SourceMeta;
}

export interface DataSource {
  readonly name: string;
  fetch(req: SourceRequest): Promise<SourceResult>;
}

/**
 * ============================================================================
 * Execution kernel runtime types (src/kernel/)
 * ============================================================================
 */

export interface CompositeRequest {
  /** Generated per request; identical token passed to every source. */
  runId: string;
  snapshotToken: string | null;
  /** Absolute epoch-ms deadline for the whole composite request. */
  deadline: number;
  params: Record<string, unknown>;
}

export type NodeStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped' // upstream required dependency failed
  | 'timed-out'
  | 'cancelled';

export interface NodeExecutionRecord {
  nodeId: string;
  source: string;
  status: NodeStatus;
  /** How many times the source was actually invoked (cancellation may race). */
  attempts: number;
  startedAt?: number;
  finishedAt?: number;
  latencyMs?: number;
  result?: SourceResult;
  failure?: FailureDetail;
  /** Snapshot token actually delivered to the source. */
  snapshotTokenUsed: string | null;
  snapshotHonoured: boolean;
  /** Effective absolute deadline the node ran under. */
  deadline: number;
  /** true when the kernel rejected the call without invoking the source. */
  didNotInvoke: boolean;
}

export type FieldStatus =
  | 'present' // value resolved from a successful source/constant/derivation
  | 'missing-required-failed' // required dependency failed
  | 'missing-optional-default' // optional dependency failed → fallback applied
  | 'missing-optional-skipped' // optional dependency failed, no fallback → null
  | 'missing-upstream-skipped' // field's node was never called
  | 'derivation-failed';

export interface FieldResult {
  path: string;
  required: boolean;
  status: FieldStatus;
  value?: unknown;
  /** Provenance: which node/property (or constant/derivation) produced it. */
  source?: string;
  reasons: FailureDetail[];
}

export type RunOutcome =
  | 'complete' // every required field present
  | 'partial' // some required fields missing, response still assembled
  | 'failed'; // request-level failure before/around execution

export interface ConsistencyNote {
  kind: 'snapshot-unsupported' | 'snapshot-mismatch' | 'snapshot-not-requested';
  nodeId: string;
  source: string;
  detail: string;
}

export interface CompositeResponse {
  runId: string;
  outcome: RunOutcome;
  contract: { name: string; version: number };
  snapshotToken: string | null;
  dataVersion: { requested: string | null; observed: string[]; consistent: boolean };
  data: Record<string, unknown>;
  fields: FieldResult[];
  nodeResults: NodeExecutionRecord[];
  consistency: ConsistencyNote[];
  failures: FailureDetail[];
  startedAt: number;
  finishedAt: number;
  durationMs: number;
}
