/**
 * JSON-RPC 2.0 protocol types (per https://www.jsonrpc.org/specification).
 *
 * These types are transport-agnostic: neither HTTP nor SQLite appears here.
 */

export const JSONRPC = '2.0' as const;

/** Standard JSON-RPC error codes. */
export const ErrorCode = {
  PARSE_ERROR: -32700,
  INVALID_REQUEST: -32600,
  METHOD_NOT_FOUND: -32601,
  INVALID_PARAMS: -32602,
  INTERNAL_ERROR: -32603,
  // -32000..-32099 is the reserved range for implementation-defined server errors.
  SERVER_ERROR_DUPLICATE_ID: -32001,
  SERVER_ERROR_METHOD_FAILED: -32004,
  SERVER_ERROR_IDEMPOTENCY_CONFLICT: -32005,
  SERVER_ERROR_NOT_FOUND: -32006,
  SERVER_ERROR_CANCELLED: -32007,
  SERVER_ERROR_ABORTED: -32008,
} as const;

/** A request carrying an id MUST get a response; a notification (no id) MUST NOT. */
export type RequestId = string | number | null;

/**
 * The raw shape we accept after JSON.parse. JSON values are structurally
 * unknowable, so everything downstream goes through validators rather than
 * casts. `unknown` on purpose.
 */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface RpcRequest {
  jsonrpc: '2.0';
  method: string;
  params?: JsonValue;
  /** Absent (NOT null) means "notification". Null is still a request with id null. */
  id?: RequestId;
}

/** Result of classifying a single parsed value. */
export type ParsedMessage =
  | { kind: 'request'; request: RpcRequest; isNotification: false; id: RequestId }
  | { kind: 'notification'; request: RpcRequest; isNotification: true }
  | {
      kind: 'invalid';
      isNotification: false;
      /** id per spec "if there is an id", echoed back if usable, else null. */
      id: RequestId;
      code: number;
      message: string;
      /** Distinguishes an invalid envelope (-32600) from a bad batch element. */
      reason: 'invalid-request' | 'invalid-batch-element';
    };

export interface RpcSuccess {
  jsonrpc: '2.0';
  result: JsonValue;
  id: RequestId;
}

export interface RpcErrorObject {
  code: number;
  message: string;
  data?: JsonValue;
}

export interface RpcFailure {
  jsonrpc: '2.0';
  error: RpcErrorObject;
  id: RequestId;
}

export type RpcResponse = RpcSuccess | RpcFailure;

export function isFailure(response: RpcResponse): response is RpcFailure {
  return 'error' in response;
}
