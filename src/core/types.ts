/**
 * Canonical domain types shared by parser, kernel, store and diagnostics.
 *
 * The parser turns raw OpenAPI 3.1 documents into {@link NormalizedContract}s;
 * the kernel compares two normalized contracts and never touches raw JSON.
 */

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

/** JSON-Schema-ish type tag used inside the supported subset. */
export type SchemaType =
  | 'string'
  | 'number'
  | 'integer'
  | 'boolean'
  | 'object'
  | 'array'
  | 'null';

/**
 * Normalized JSON Schema (subset of OpenAPI 3.1's dialect).
 * `x-*` extensions are kept verbatim in {@link extensions} and never judged.
 */
export interface NormalizedSchema {
  /** Ordered list of type tags; 3.1 allows `type: ["string","null"]`. */
  types: SchemaType[];
  enum: JsonValue[] | null;
  /** Presence distinguishes "no default" (undefined) from default: null. */
  hasDefault: boolean;
  default: JsonValue | undefined;
  format: string | null;
  /** object properties keyed by property name (insertion order preserved). */
  properties: Map<string, NormalizedSchema>;
  required: Set<string>;
  /** array item schema; absent/unknown items -> null (treated as any). */
  items: NormalizedSchema | null;
  /** Bounded resolution diagnostics collected while resolving $ref. */
  refTrail: string[];
  /** Raw `x-*` members — unknown, never compared for compatibility. */
  extensions: Record<string, JsonValue>;
}

export type ParameterLocation = 'query' | 'header' | 'path' | 'cookie';

export interface NormalizedParameter {
  name: string;
  in: ParameterLocation;
  required: boolean;
  /** Stable identity: same name in different locations are different params. */
  key: string;
  schema: NormalizedSchema;
  /** raw style/explode retained for witness rendering only */
  style: string | null;
}

export interface NormalizedRequestBody {
  required: boolean;
  /** mediaType -> schema (subset usually carries application/json only) */
  content: Map<string, NormalizedSchema>;
}

export interface NormalizedResponse {
  statusCode: string;
  description: string;
  content: Map<string, NormalizedSchema>;
}

export interface NormalizedOperation {
  method: string;
  path: string;
  operationId: string | null;
  parameters: Map<string, NormalizedParameter>;
  requestBody: NormalizedRequestBody | null;
  /** explicit status-code keyed responses, excluding `default` */
  responses: Map<string, NormalizedResponse>;
  /** presence of the `default` response catch-all */
  defaultResponse: NormalizedResponse | null;
}

export interface NormalizedContract {
  openapi: string;
  title: string;
  version: string;
  /** key `${method.toUpperCase()} ${path}` */
  operations: Map<string, NormalizedOperation>;
}

// ---------------------------------------------------------------------------
// Finding / witness types
// ---------------------------------------------------------------------------

export type Direction = 'request' | 'response';

/**
 * Failure categories. Every finding maps to exactly one so tests can assert
 * the concrete class of incompatibility rather than a free-text message.
 */
export type FailureCode =
  | 'OPERATION_REMOVED'
  | 'OPERATION_ADDED'
  | 'REQUIRED_PARAM_ADDED'
  | 'OPTIONAL_PARAM_ADDED'
  | 'PARAM_REMOVED'
  | 'PARAM_LOCATION_CHANGED'
  | 'PARAM_BECAME_REQUIRED'
  | 'PARAM_BECAME_OPTIONAL'
  | 'PARAM_TYPE_NARROWED'
  | 'PARAM_ENUM_NARROWED'
  | 'PARAM_ENUM_EXTENDED'
  | 'PARAM_ENUM_RELAXED'
  | 'PARAM_DEFAULT_CHANGED'
  | 'PARAM_DEFAULT_REMOVED'
  | 'PARAM_DEFAULT_ADDED'
  | 'REQUEST_BODY_BECAME_REQUIRED'
  | 'REQUEST_BODY_ADDED'
  | 'REQUEST_BODY_REMOVED'
  | 'REQUEST_BODY_TYPE_NARROWED'
  | 'REQUEST_BODY_ENUM_NARROWED'
  | 'REQUEST_BODY_ENUM_EXTENDED'
  | 'REQUEST_BODY_ENUM_RELAXED'
  | 'REQUEST_BODY_DEFAULT_CHANGED'
  | 'REQUEST_BODY_DEFAULT_REMOVED'
  | 'REQUEST_BODY_DEFAULT_ADDED'
  | 'REQUEST_BODY_REQUIRED_FIELD_ADDED'
  | 'REQUEST_BODY_OPTIONAL_FIELD_ADDED'
  | 'REQUEST_BODY_FIELD_REMOVED'
  | 'REQUEST_BODY_FIELD_BECAME_REQUIRED'
  | 'REQUEST_BODY_FIELD_BECAME_OPTIONAL'
  | 'REQUEST_BODY_FIELD_TYPE_NARROWED'
  | 'REQUEST_BODY_FIELD_ENUM_NARROWED'
  | 'REQUEST_BODY_FIELD_ENUM_EXTENDED'
  | 'REQUEST_BODY_FIELD_ENUM_RELAXED'
  | 'REQUEST_BODY_FIELD_DEFAULT_CHANGED'
  | 'REQUEST_BODY_FIELD_DEFAULT_REMOVED'
  | 'REQUEST_BODY_FIELD_DEFAULT_ADDED'
  | 'RESPONSE_STATUS_REMOVED'
  | 'RESPONSE_STATUS_ADDED'
  | 'RESPONSE_TYPE_NARROWED'
  | 'RESPONSE_ENUM_NARROWED'
  | 'RESPONSE_ENUM_EXTENDED'
  | 'RESPONSE_ENUM_RELAXED'
  | 'RESPONSE_REQUIRED_FIELD_REMOVED'
  | 'RESPONSE_OPTIONAL_FIELD_REMOVED'
  | 'RESPONSE_FIELD_ADDED'
  | 'RESPONSE_FIELD_BECAME_OPTIONAL'
  | 'RESPONSE_FIELD_BECAME_REQUIRED'
  | 'RESPONSE_FIELD_TYPE_NARROWED'
  | 'RESPONSE_FIELD_ENUM_NARROWED'
  | 'RESPONSE_FIELD_ENUM_EXTENDED'
  | 'RESPONSE_FIELD_ENUM_RELAXED'
  | 'RESPONSE_FIELD_DEFAULT_CHANGED';

export type Severity = 'BREAKING' | 'NON_BREAKING' | 'UNCERTAIN';

/**
 * A concrete, minimal value witnessing the verdict.
 * Request witnesses are payloads an OLD client could send that the NEW server
 * rejects; response witnesses are payloads the NEW server could return that an
 * OLD client cannot consume.
 */
export interface Witness {
  /** e.g. `GET /pets?limit=10`, JSON pointer fragment, status code */
  location: string;
  /** minimal concrete example value */
  example: JsonValue;
  /** human explanation of why the two sides disagree at this location */
  rationale: string;
}

export interface Finding {
  id: string;
  direction: Direction;
  code: FailureCode;
  severity: Severity;
  /** operation key, or null for document-level issues */
  operation: string | null;
  /** JSON-pointer-ish path inside the operation, for log correlation */
  path: string;
  message: string;
  oldValue: JsonValue | undefined;
  newValue: JsonValue | undefined;
  witness: Witness | null;
}

/** Things the analyzer could not judge — surfaced separately, never silent. */
export interface Uncertainty {
  code:
    | 'UNRESOLVED_REF'
    | 'REF_DEPTH_LIMIT'
    | 'REF_CYCLE'
    | 'UNSUPPORTED_KEYWORD'
    | 'UNSUPPORTED_SPEC_VERSION'
    | 'AMBIGUOUS_MEDIA_TYPE'
    | 'INVALID_DOCUMENT';
  location: string;
  detail: string;
}

export interface DiffResult {
  /** request id echoed by the diagnostics layer */
  requestId: string;
  oldTitle: string;
  newTitle: string;
  oldVersion: string;
  newVersion: string;
  compatible: boolean;
  /** minimal set: one witness per distinct root cause, BREAKING first */
  findings: Finding[];
  uncertainties: Uncertainty[];
  /** ordered trace of the key analysis steps (correlatable in logs) */
  steps: TraceStep[];
  stats: {
    operationsCompared: number;
    operationsAdded: number;
    operationsRemoved: number;
    breaking: number;
    nonBreaking: number;
    uncertain: number;
  };
}

export interface TraceStep {
  index: number;
  phase: 'parse-old' | 'parse-new' | 'compare' | 'witness' | 'finalize';
  location: string;
  detail: string;
}
