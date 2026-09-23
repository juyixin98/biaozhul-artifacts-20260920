/**
 * Types for the Solidity compiler `storageLayout` JSON output and for the
 * compatibility report produced by this checker.
 *
 * The shapes below mirror what `solc` emits under
 * `output.contracts[file][name].storageLayout` (and what Foundry/Hardhat
 * surface as `storageLayout` in build artifacts).
 */

/** A single state-variable entry in `storageLayout.storage`. */
export interface StorageEntry {
  astId: number;
  contract: string;
  label: string;
  offset: number;
  slot: string;
  type: string;
}

/** A struct member as found in `storageLayout.types[*].members`. */
export interface StructMember {
  astId: number;
  contract: string;
  label: string;
  offset: number;
  slot: string;
  type: string;
}

/** A type descriptor in `storageLayout.types`. */
export interface TypeEntry {
  encoding: 'inplace' | 'mapping' | 'dynamic_array' | 'bytes' | string;
  label: string;
  numberOfBytes: string;
  /** static/dynamic array element type id */
  base?: string;
  /** mapping key type id */
  key?: string;
  /** mapping value type id */
  value?: string;
  /** struct members */
  members?: StructMember[];
}

/** The compiler storageLayout object. */
export interface StorageLayout {
  storage: StorageEntry[];
  types: Record<string, TypeEntry>;
}

/** Severity of a single finding. `error` breaks compatibility. */
export type Severity = 'error' | 'warning' | 'info';

/** Overall verdict of a comparison. */
export type Verdict = 'compatible' | 'incompatible' | 'unknown';

/**
 * Machine-readable finding codes.
 *
 * Incompatible (error):
 *  - FIELD_REMOVED            an old variable is gone from the new layout
 *  - FIELD_MOVED              slot/offset changed (includes inheritance reordering)
 *  - FIELD_INSERTED           a new variable sits before/between old variables
 *  - TYPE_KIND_CHANGED        encoding changed (e.g. value type -> mapping)
 *  - TYPE_WIDTH_CHANGED       numberOfBytes changed for a value type
 *  - TYPE_LABEL_CHANGED       value type label changed (e.g. uint128 -> int128)
 *  - STRUCT_MEMBER_REMOVED    a struct member disappeared
 *  - STRUCT_MEMBER_MOVED      a struct member changed slot/offset
 *  - STRUCT_MEMBER_INSERTED   a struct member was added before existing members
 *  - ARRAY_LENGTH_CHANGED     static array length changed
 *  - ARRAY_BASE_CHANGED       array element type changed incompatibly
 *  - MAPPING_TYPE_CHANGED     mapping key/value type changed incompatibly
 *  - GAP_OVERCONSUMED         new fields use more slots than the gap provided
 *  - GAP_MISMATCH             gap shrink does not match slots consumed by new fields
 *
 * Conservative (unknown):
 *  - UNKNOWN_TYPE             a referenced type id is missing from `types`
 *  - UNKNOWN_ENCODING         an encoding this checker does not understand
 *
 * Informational (compatible rules):
 *  - FIELD_APPENDED           new variable appended after all old variables
 *  - STRUCT_MEMBER_APPENDED   new struct member appended after old members
 *  - GAP_REDUCED              storage gap shrunk to make room for appended fields
 */
export type FindingCode =
  | 'FIELD_REMOVED'
  | 'FIELD_MOVED'
  | 'FIELD_INSERTED'
  | 'TYPE_KIND_CHANGED'
  | 'TYPE_WIDTH_CHANGED'
  | 'TYPE_LABEL_CHANGED'
  | 'STRUCT_MEMBER_REMOVED'
  | 'STRUCT_MEMBER_MOVED'
  | 'STRUCT_MEMBER_INSERTED'
  | 'ARRAY_LENGTH_CHANGED'
  | 'ARRAY_BASE_CHANGED'
  | 'MAPPING_TYPE_CHANGED'
  | 'GAP_OVERCONSUMED'
  | 'GAP_MISMATCH'
  | 'UNKNOWN_TYPE'
  | 'UNKNOWN_ENCODING'
  | 'FIELD_APPENDED'
  | 'STRUCT_MEMBER_APPENDED'
  | 'GAP_REDUCED';

/** Evidence pinned to one side of the comparison. */
export interface SideEvidence {
  slot?: string;
  offset?: number;
  type?: string;
  typeLabel?: string;
  numberOfBytes?: string;
}

export interface Finding {
  severity: Severity;
  code: FindingCode;
  /**
   * Shortest path to the divergence, e.g.
   * `Token.balances.value` or `Token.meta.owner`.
   */
  path: string;
  message: string;
  evidence: {
    old?: SideEvidence;
    new?: SideEvidence;
  };
}

export interface CompareReport {
  verdict: Verdict;
  /** human-readable one-line summary */
  summary: string;
  findings: Finding[];
  /** counts by severity, for quick triage */
  stats: { errors: number; warnings: number; infos: number };
}
