/**
 * Contract parsing for JSON-RPC 2.0.
 *
 * Two distinct failure layers are modeled as different result shapes rather
 * than exceptions thrown across layers:
 *
 *   1. parse_error   (-32700): raw text is not valid JSON or top level is
 *                    neither object nor array. The request id is unknowable,
 *                    so the response id is `null`.
 *   2. invalid_request (-32600): JSON parsed but the envelope violates the
 *                    contract (bad jsonrpc/method/id). Per-element invalid
 *                    entries inside a batch each produce their own error
 *                    response with id `null`.
 *
 * A batch that parses to `[]` is an INVALID Request (non-empty requirement)
 * and yields a single error object — it is NOT "all notifications" and must
 * not produce an empty-array HTTP body. A single parsed object is returned as
 * { kind: "single" } so callers cannot confuse the two shapes.
 */

import {
  INVALID_REQUEST,
  PARSE_ERROR,
  STABLE_MESSAGES,
} from "./errors.js";
import type { FailureCategory, RpcId, RpcRequest } from "./types.js";

export interface ValidMessage {
  readonly kind: "message";
  readonly request: RpcRequest;
  /** Echoed back verbatim in error.data so ledger rows and responses correlate. */
  readonly position: number;
}

export interface InvalidMessage {
  readonly kind: "invalid";
  readonly position: number;
  readonly code: number;
  readonly category: FailureCategory;
  readonly message: string;
}

export type BatchEntry = ValidMessage | InvalidMessage;

export type ParseOutcome =
  | { readonly kind: "single"; readonly entry: BatchEntry }
  | { readonly kind: "batch"; readonly entries: readonly BatchEntry[] }
  | {
      readonly kind: "parseError";
      readonly code: typeof PARSE_ERROR;
      readonly category: "parse_error";
      readonly message: string;
      readonly reason: string;
    }
  | {
      readonly kind: "emptyBatch";
      readonly code: typeof INVALID_REQUEST;
      readonly category: "invalid_request";
      readonly message: string;
      readonly reason: string;
    }
  | {
      readonly kind: "invalidEnvelope";
      readonly code: typeof INVALID_REQUEST;
      readonly category: "invalid_request";
      readonly message: string;
      readonly reason: string;
    };

function isValidId(value: unknown): value is RpcId {
  if (value === null) return true;
  if (typeof value === "string") return true;
  if (typeof value === "number") return Number.isFinite(value);
  return false;
}

/**
 * Validate one decoded JSON value against the request envelope.
 * Returns an RpcRequest only when every contract rule holds.
 */
function validateEnvelope(value: unknown, position: number): BatchEntry {
  const reject = (reason: string): InvalidMessage => ({
    kind: "invalid",
    position,
    code: INVALID_REQUEST,
    category: "invalid_request",
    message: `${STABLE_MESSAGES.invalidRequest}: ${reason}`,
  });

  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return reject("request must be a JSON object");
  }
  const obj = value as Record<string, unknown>;

  if (obj.jsonrpc !== "2.0") {
    return reject('jsonrpc must be the string "2.0"');
  }
  if (typeof obj.method !== "string" || obj.method.length === 0) {
    return reject("method must be a non-empty string");
  }
  if ("id" in obj && !isValidId(obj.id)) {
    return reject("id must be a string, number, null, or absent");
  }
  if (
    "params" in obj &&
    obj.params !== null &&
    typeof obj.params !== "object"
  ) {
    // Spec allows params to be array or object; anything else is invalid.
    return reject("params must be an array or object");
  }

  const request: RpcRequest = {
    jsonrpc: "2.0",
    method: obj.method,
    ...(("params" in obj ? { params: obj.params } : {})),
    ...(("id" in obj ? { id: obj.id as RpcId } : {})),
  };
  return { kind: "message", request, position };
}

export function parseRpcPayload(raw: string): ParseOutcome {
  let decoded: unknown;
  try {
    decoded = JSON.parse(raw);
  } catch (cause) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    return {
      kind: "parseError",
      code: PARSE_ERROR,
      category: "parse_error",
      message: STABLE_MESSAGES.parseError,
      reason,
    };
  }

  if (Array.isArray(decoded)) {
    if (decoded.length === 0) {
      return {
        kind: "emptyBatch",
        code: INVALID_REQUEST,
        category: "invalid_request",
        message: STABLE_MESSAGES.emptyBatch,
        reason: "received []",
      };
    }
    return {
      kind: "batch",
      entries: decoded.map((item, index) => validateEnvelope(item, index)),
    };
  }

  if (typeof decoded !== "object" || decoded === null) {
    // Top-level scalar (e.g. "4", '"x"', "true") is syntactically valid JSON
    // but not a Request object: a CONTRACT failure, not a parse failure.
    return {
      kind: "invalidEnvelope",
      code: INVALID_REQUEST,
      category: "invalid_request",
      message: STABLE_MESSAGES.invalidRequest,
      reason: "top-level JSON value is neither object nor array",
    };
  }

  return { kind: "single", entry: validateEnvelope(decoded, 0) };
}
