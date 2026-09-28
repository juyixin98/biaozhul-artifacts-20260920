/**
 * Canonical JSON serialization and content hashing for version/ETag
 * derivation. Object keys are sorted recursively so that semantically equal
 * documents hash identically regardless of insertion order.
 */
import { createHash } from 'node:crypto';

export function stableStringify(value: unknown): string {
  return JSON.stringify(sortDeep(value));
}

function sortDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortDeep);
  if (value !== null && typeof value === 'object') {
    const source = value as Record<string, unknown>;
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(source).sort()) {
      sorted[key] = sortDeep(source[key]);
    }
    return sorted;
  }
  return value;
}

/** First 8 hex chars of SHA-256 over the canonical encoding of `value`. */
export function contentHash(value: unknown): string {
  return createHash('sha256').update(stableStringify(value)).digest('hex').slice(0, 8);
}
