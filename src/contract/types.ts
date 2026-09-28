/**
 * Normalized contract model for the supported OpenAPI 3.1 subset.
 *
 * Supported schema keywords: type (incl. "null" union arrays), enum, const,
 * properties, required, items, additionalProperties, default, $ref (bounded).
 * Everything else is either an ignored annotation (description, title,
 * examples, deprecated) or an *unknown constraint* that downgrades affected
 * conclusions to `undetermined`.
 */

export type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };
export type JsonObject = { [k: string]: JsonValue };

/** Raw JSON-Schema-ish node as found in the document (refs not inlined). */
export type RawSchema = JsonObject | boolean;

export type ParamLocation = 'query' | 'header' | 'path' | 'cookie';

export interface ParamModel {
  name: string;
  in: ParamLocation;
  required: boolean;
  schema?: RawSchema;
  defaultValue?: JsonValue;
  hasDefault: boolean;
}

export interface BodyModel {
  required: boolean;
  contentType: string;
  schema?: RawSchema;
}

export interface ResponseModel {
  status: string; // "200", "404", "default", ...
  contentType?: string;
  schema?: RawSchema;
}

export interface OperationModel {
  id: string; // "GET /pets"
  method: string;
  path: string;
  parameters: ParamModel[];
  requestBody?: BodyModel;
  responses: ResponseModel[];
}

export interface ContractModel {
  title: string;
  version: string;
  operations: OperationModel[];
  /** x-* extension keys seen at the root of the document. */
  extensions: string[];
}

/** Keywords the kernel understands and compares structurally. */
export const KNOWN_SCHEMA_KEYWORDS = new Set([
  'type',
  'enum',
  'const',
  'properties',
  'required',
  'items',
  'additionalProperties',
  '$ref',
]);

/** Keywords treated as annotations: compared for behavior notes, never breaking. */
export const ANNOTATION_KEYWORDS = new Set([
  'default',
  'description',
  'title',
  'examples',
  'deprecated',
  'readOnly',
  'writeOnly',
  'nullable', // 3.0 leftover: surfaced as undetermined, not interpreted
]);

/** Narrow a parsed JSON value to a schema node (object or boolean). */
export function asSchema(v: JsonValue | undefined): RawSchema | undefined {
  if (typeof v === 'boolean') return v;
  if (typeof v === 'object' && v !== null && !Array.isArray(v)) return v;
  return undefined;
}
