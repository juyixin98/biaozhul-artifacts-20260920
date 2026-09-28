/**
 * Correlation identifiers. These identify OUR records (request/call rows);
 * they are never used as client-facing idempotency keys and never replace the
 * JSON-RPC request id, which belongs to the transport correlation.
 */

import { randomUUID } from "node:crypto";

export function newRequestCorr(): string {
  return `req_${randomUUID()}`;
}

export function newCallCorr(): string {
  return `call_${randomUUID()}`;
}

/** Stable JSON encoding of an RPC id so 1 and "1" and null never collide. */
export function encodeRpcId(id: string | number | null | undefined): string | null {
  return id === undefined ? null : JSON.stringify(id);
}
