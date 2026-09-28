/**
 * JSON-RPC 2.0 wire types (https://www.jsonrpc.org/specification).
 *
 * A request that carries an `id` member (including `null`) expects a response.
 * A request WITHOUT an `id` member is a notification and never gets a response.
 * `id: null` is therefore deliberately NOT a notification.
 */

export type RpcId = string | number | null;

export interface RpcRequest {
  jsonrpc: "2.0";
  method: string;
  params?: unknown;
  id?: RpcId;
}

export interface RpcSuccessResponse {
  jsonrpc: "2.0";
  result: unknown;
  id: RpcId;
}

export interface RpcErrorObject {
  code: number;
  message: string;
  data?: {
    readonly category: FailureCategory;
    readonly correlationId?: string;
    readonly detail?: unknown;
  };
}

export interface RpcErrorResponse {
  jsonrpc: "2.0";
  error: RpcErrorObject;
  id: RpcId;
}

export type RpcResponse = RpcSuccessResponse | RpcErrorResponse;

export function isNotification(req: RpcRequest): boolean {
  return !("id" in req);
}

/**
 * Failure categories used in `error.data.category` and in the ledger.
 * Parse/transport failures, contract failures and business failures are kept
 * in distinct layers so a malformed envelope is never reported as a business
 * error and vice versa.
 */
export type FailureCategory =
  | "parse_error"
  | "invalid_request"
  | "method_not_found"
  | "invalid_params"
  | "business_rule"
  | "not_found"
  | "internal"
  | "interrupted"
  | "notification_execution_failed";
