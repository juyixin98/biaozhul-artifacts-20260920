/**
 * Composition contract — the declarative dependency graph.
 *
 * A contract binds output fields to source calls. Every field declares:
 *  - `required`: a failed REQUIRED field fails the whole composite;
 *                an OPTIONAL field degrades to a field-level reason.
 *  - `source`:   which local source produces it.
 *
 * Calls may depend on other calls (`dependencies`); the engine schedules a call
 * only after every dependency has resolved, and propagates dependency values
 * through the `map`/`buildRequest` functions.
 */
import type { ErrorDetail } from '../kernel/errors.js';

export type FieldRequirement = 'required' | 'optional';

export interface CallSpec {
  /** Unique call node id inside one contract. */
  id: string;
  /** Source registry name, e.g. `users`. */
  source: string;
  /** Source-side operation, e.g. `getProfile`. */
  method: string;
  /** Ids of calls that must resolve before this one. */
  dependencies: string[];
  /**
   * Build the source request from validated request input and resolved
   * dependency results (map dependencyId -> dependency value).
   */
  buildRequest: (deps: Record<string, unknown>, input: Record<string, string | number>) => unknown;
  /**
   * Fields this call contributes to the response, extracted from the raw source
   * value. `path` is a dotted path; missing values honor the field requirement.
   */
  fields: FieldBinding[];
}

export interface FieldBinding {
  /** Output field name in the final typed response. */
  output: string;
  requirement: FieldRequirement;
  /** Dotted path inside the source result; defaults to the field name. */
  path?: string;
  /** Static default applied when an OPTIONAL field is absent (not on failure). */
  defaultOnMissing?: unknown;
}

export interface CompositeContract {
  name: string;
  /** Input schema, validated at the boundary (see contract/validate.ts). */
  input: InputField[];
  calls: CallSpec[];
}

export interface InputField {
  name: string;
  required: boolean;
  type: 'string' | 'number';
}

/** Per-field outcome carried in a partial response. */
export interface FieldStatus {
  field: string;
  source: string;
  call: string;
  requirement: FieldRequirement;
  state: 'present' | 'missing' | 'failed';
  /** Present when state === 'failed' (or 'missing' for a required field). */
  reason?: ErrorDetail;
  /** True when an optional field was filled from its declared default. */
  defaulted?: boolean;
}
