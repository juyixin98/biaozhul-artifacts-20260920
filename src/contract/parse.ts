import {
  ErrorCode,
  type JsonValue,
  type ParsedMessage,
  type RpcRequest,
} from './protocol.js';

/**
 * Contract parsing, deliberately kept separate from execution.
 *
 * Layering enforced here:
 *  - PARSE_ERROR (-32700): the body is not a single JSON value at all. There
 *    is no trustworthy envelope, so no batch/request semantics apply.
 *  - INVALID_REQUEST (-32600): valid JSON, but not a JSON-RPC 2.0 envelope
 *    (single object case), or an empty batch `[]`.
 *  - invalid batch element (also -32600 at the element level via
 *    SERVER_ERROR_BATCH_ELEMENT data): inside a non-empty batch, one element
 *    is not a valid Request object. Per spec each such element produces its
 *    own response; we tag the reason so the kernel can tell accept / reject /
 *    indeterminate apart in diagnostics.
 */

export type ParseOutcome =
  | { ok: true; topLevel: 'single' | 'batch'; messages: ParsedMessage[] }
  | {
      ok: false;
      code: typeof ErrorCode.PARSE_ERROR;
      message: string;
      /** True when rejection is due to the size cap rather than bad syntax. */
      oversized: boolean;
      /**
       * Byte length of the rejected body. We deliberately do NOT keep a raw
       * substring: malformed JSON can still contain secret-bearing string
       * values, and persisting a preview would leak them into diagnostics.
       */
      rawByteLength: number;
    };

const MAX_BODY_BYTES = 1_048_576; // 1 MiB hard cap on a request body.

export function parseEnvelope(rawText: string): ParseOutcome {
  const length = byteLength(rawText);
  let value: unknown;
  try {
    // JSON.parse has no size knob; guard on UTF-8 BYTE length (not UTF-16
    // char count) before handing it the string.
    if (length > MAX_BODY_BYTES) {
      return {
        ok: false,
        code: ErrorCode.PARSE_ERROR,
        message: 'Parse error: request body exceeds 1 MiB limit',
        oversized: true,
        rawByteLength: length,
      };
    }
    value = JSON.parse(rawText);
  } catch (err) {
    void err;
    return {
      ok: false,
      code: ErrorCode.PARSE_ERROR,
      // The raw engine message is intentionally not retained: it can embed a
      // source excerpt. The kernel emits a fixed response and only logs the
      // byte length. `oversized` distinguishes the two causes.
      message: 'Parse error: body is not valid JSON',
      oversized: false,
      rawByteLength: length,
    };
  }

  if (Array.isArray(value)) {
    return { ok: true, topLevel: 'batch', messages: value.map(classifyBatchElement) };
  }
  const classified = classifySingle(value);
  if (classified.kind === 'invalid' && classified.reason === 'invalid-batch-element') {
    // classifySingle never emits that tag; normalize for the single path.
    return {
      ok: true,
      topLevel: 'single',
      messages: [{ ...classified, reason: 'invalid-request' }],
    };
  }
  return { ok: true, topLevel: 'single', messages: [classified] };
}

/** True only when the top-level JSON is `[]` — distinct from a one-element batch. */
export function isEmptyBatch(messages: ParsedMessage[]): boolean {
  return messages.length === 0;
}

function classifySingle(value: unknown): ParsedMessage {
  const problem = validateRequestObject(value);
  if (problem) {
    return {
      kind: 'invalid',
      isNotification: false,
      id: extractId(value),
      code: ErrorCode.INVALID_REQUEST,
      message: problem,
      reason: 'invalid-request',
    };
  }
  return toMessage(value as RpcRequest);
}

function classifyBatchElement(value: unknown): ParsedMessage {
  const problem = validateRequestObject(value);
  if (problem) {
    return {
      kind: 'invalid',
      isNotification: false,
      id: extractId(value),
      code: ErrorCode.INVALID_REQUEST,
      message: problem,
      reason: 'invalid-batch-element',
    };
  }
  return toMessage(value as RpcRequest);
}

function toMessage(req: RpcRequest): ParsedMessage {
  if (!Object.prototype.hasOwnProperty.call(req, 'id')) {
    return { kind: 'notification', request: req, isNotification: true };
  }
  return { kind: 'request', request: req, isNotification: false, id: req.id as RequestId2 };
}

type RequestId2 = string | number | null;

/**
 * Validate one would-be Request object. Returns an error message, or null.
 *
 * Tricky cases covered explicitly:
 *  - `id` present but boolean/object/array -> INVALID_REQUEST, echo null.
 *  - `params` present but not object/array -> INVALID_REQUEST (spec 4.2).
 *  - extra members are allowed by spec; we tolerate them.
 *  - numeric ids that are not safe integers are still legal JSON-RPC ids but
 *    get normalized to their string form? No — spec says Number; we reject
 *    non-finite and non-integer floats to avoid cross-language drift.
 */
function validateRequestObject(value: unknown): string | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return 'Invalid Request: JSON-RPC message must be an object';
  }
  const obj = value as Record<string, unknown>;
  if (obj.jsonrpc !== '2.0') {
    return "Invalid Request: member \"jsonrpc\" must be exactly the string \"2.0\"";
  }
  if (typeof obj.method !== 'string' || obj.method.length === 0) {
    return 'Invalid Request: member "method" must be a non-empty string';
  }
  if (obj.params !== undefined && !isStructuredClonerOk(obj.params)) {
    return 'Invalid Request: member "params" must be an array or object';
  }
  if (Object.prototype.hasOwnProperty.call(obj, 'id')) {
    const id = obj.id;
    if (id !== null && typeof id !== 'string' && typeof id !== 'number') {
      return 'Invalid Request: member "id" must be string, number, null, or absent';
    }
    if (typeof id === 'number' && (!Number.isFinite(id) || !Number.isSafeInteger(id))) {
      return 'Invalid Request: numeric "id" must be a finite safe integer';
    }
  }
  return null;
}

function isStructuredClonerOk(params: unknown): boolean {
  return typeof params === 'object' && params !== null;
}

/**
 * Extract an id for error echo per spec: "If there was an error in detecting
 * the id in the Request object, it MUST be Null."
 */
function extractId(value: unknown): RequestId2 {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  const id = (value as Record<string, unknown>).id;
  if (id === null || typeof id === 'string') return id;
  if (typeof id === 'number' && Number.isFinite(id) && Number.isSafeInteger(id)) return id;
  return null;
}

function byteLength(raw: string): number {
  // UTF-8 byte count; Buffer is available in Node. Falls back gracefully.
  return typeof Buffer !== 'undefined'
    ? Buffer.byteLength(raw, 'utf8')
    : new TextEncoder().encode(raw).length;
}

export type { JsonValue };
