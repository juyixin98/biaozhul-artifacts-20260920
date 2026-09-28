/**
 * Output types of the diff kernel.
 *
 * A finding is always directional:
 *  - REQUEST direction = "a request accepted by the OLD contract may be
 *    rejected by the NEW server" (server made its input stricter / moved things).
 *  - RESPONSE direction = "a response accepted by the NEW contract may be
 *    rejected by an OLD client" (server changed what it returns).
 */
import type { JsonValue, RawSchema } from '../contract/types.js';

export type Direction = 'request' | 'response';

/**
 * Fixed failure taxonomy. Tests assert on these exact categories.
 */
export type FailureCategory =
  | 'OPERATION_REMOVED'
  | 'PARAM_LOCATION_CHANGED'
  | 'PARAM_MADE_REQUIRED'
  | 'PARAM_REMOVED'
  | 'REQUEST_BODY_MADE_REQUIRED'
  | 'REQUEST_BODY_REMOVED'
  | 'TYPE_NARROWED'
  | 'ENUM_NARROWED'
  | 'NULLABILITY_REMOVED'
  | 'CONST_CHANGED'
  | 'PROPERTY_MADE_REQUIRED'
  | 'PROPERTY_REMOVED'
  | 'ADDITIONAL_PROPERTIES_TIGHTENED'
  | 'STATUS_CODE_REMOVED'
  | 'STATUS_CODE_SPLIT'
  | 'RESPONSE_CONTENT_TYPE_REMOVED'
  | 'DEFAULT_VALUE_CHANGED'
  | 'UNKNOWN_EXTENSION_PRESENT'
  | 'UNKNOWN_KEYWORD'
  | 'UNRESOLVED_REF'
  | 'CYCLIC_REF';

export type Severity = 'breaking' | 'undetermined' | 'informational';

export interface Witness {
  /** Human description of what the witness is, e.g. "GET /pets?tag=cat". */
  description: string;
  /**
   * Concrete minimal example. For request findings it is a request fragment
   * ({ method, path, query, headers, body }); for response findings a
   * response fragment ({ status, headers, body }).
   */
  value: JsonValue;
  /** Why the OLD side accepts it. */
  acceptedBy: string;
  /** Why/where the NEW side rejects it (or `uncertain` for undetermined). */
  rejectedBy: string;
}

export interface Finding {
  id: string; // stable id, e.g. "F003"
  direction: Direction;
  category: FailureCategory;
  severity: Severity;
  operation: string; // operation id
  /** JSON-pointer-ish location inside the contract, e.g. "parameters[query:tag].schema.enum". */
  location: string;
  message: string;
  oldSide: string; // concise description of old contract fact
  newSide: string; // concise description of new contract fact
  witness: Witness;
  /** Present when severity === 'undetermined': reason no definite verdict exists. */
  uncertainty?: string;
}

export interface DiffResult {
  requestFindings: Finding[];
  responseFindings: Finding[];
  /** x-* extensions that appeared/disappeared — never judged breaking. */
  extensionNotes: { extension: string; change: 'added' | 'removed' }[];
  compatible: boolean;
}

/** Pair of raw schemas pinned to their owning documents (refs need the doc). */
export interface SchemaPair {
  oldSchema: RawSchema | undefined;
  newSchema: RawSchema | undefined;
  oldDoc: SchemaResolver;
  newDoc: SchemaResolver;
}

/** Minimal interface the kernel needs from a document-backed ref resolver. */
export interface SchemaResolver {
  resolve(ref: string, stack: readonly string[]): { node: RawSchema | null; missing?: boolean; cycle?: boolean; external?: boolean };
}

export type { JsonValue, RawSchema };
