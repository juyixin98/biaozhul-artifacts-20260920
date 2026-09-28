/**
 * JSON-RPC 2.0 error codes and typed error hierarchy.
 *
 * Layered by origin:
 *  - parse/contract errors are constructed by the protocol parser
 *  - RpcException(INVALID_PARAMS) is raised by method parameter validation
 *  - BusinessError is raised by method business rules (server-defined -32xxx)
 *  - anything else thrown by a handler collapses to INTERNAL_ERROR
 */

import type { FailureCategory } from "./types.js";

export const PARSE_ERROR = -32700;
export const INVALID_REQUEST = -32600;
export const METHOD_NOT_FOUND = -32601;
export const INVALID_PARAMS = -32602;
export const INTERNAL_ERROR = -32603;

/** Server-defined application error range is -32000..-32099 per spec. */
export const BUSINESS_VALUE_OUT_OF_RANGE = -32001;
export const OPERATION_NOT_FOUND = -32010;
export const SYNTHETIC_BUSINESS_FAILURE = -32020;

export class RpcException extends Error {
  readonly code: number;
  readonly category: FailureCategory;
  readonly detail?: unknown;

  constructor(
    code: number,
    message: string,
    category: FailureCategory,
    detail?: unknown,
  ) {
    super(message);
    this.name = "RpcException";
    this.code = code;
    this.category = category;
    if (detail !== undefined) this.detail = detail;
  }
}

export class BusinessError extends RpcException {
  constructor(code: number, message: string, detail?: unknown) {
    super(code, message, "business_rule", detail);
    this.name = "BusinessError";
  }
}

export class NotFoundError extends RpcException {
  constructor(message: string, detail?: unknown) {
    super(OPERATION_NOT_FOUND, message, "not_found", detail);
    this.name = "NotFoundError";
  }
}

/** Messages are part of the public contract; keep them stable for tests. */
export const STABLE_MESSAGES = {
  parseError: "Parse error",
  invalidRequest: "Invalid Request",
  methodNotFound: "Method not found",
  invalidParams: "Invalid params",
  internalError: "Internal error",
  emptyBatch: "Invalid Request: batch must not be empty",
} as const;
