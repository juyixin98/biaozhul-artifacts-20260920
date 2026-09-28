import { randomBytes } from 'node:crypto';
import type { RequestId } from '../contract/protocol.js';

/**
 * Generate an independent operation identifier. Side-effectful methods always
 * get their own operation id; the JSON-RPC `id` is ONLY a response correlation
 * token and must never be used as a global idempotency key.
 */
export function newOperationId(now: () => number = Date.now): string {
  const ts = now().toString(36);
  return `op_${ts}_${randomBytes(6).toString('hex')}`;
}

export function newBatchId(now: () => number = Date.now): string {
  return `batch_${now().toString(36)}_${randomBytes(4).toString('hex')}`;
}

export function newConnectionId(): string {
  return `conn_${randomBytes(8).toString('hex')}`;
}

/**
 * Stable, type-aware key for duplicate-id detection inside ONE batch.
 *
 * Distinct types must not collide: id `1` (number) and id `"1"` (string) are
 * different requests. `null` is a legal id. Notifications have no id and are
 * not tracked here.
 */
export function duplicateKey(id: RequestId): string {
  if (id === null) return 'n:null';
  if (typeof id === 'number') return `n:${numberIdKey(id)}`;
  return `s:${id}`;
}

function numberIdKey(id: number): string {
  // 0 and -0 are the same id; finite guaranteed by parser validation.
  if (id === 0) return '0';
  return String(id);
}

/** Stable text rendering of an id for diagnostic logs (never for matching). */
export function renderId(id: RequestId | undefined): string {
  if (id === undefined) return '<notification>';
  if (id === null) return 'null';
  return typeof id === 'number' ? String(id) : JSON.stringify(id);
}
