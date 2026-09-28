/**
 * Independent test oracle.
 *
 * The canonical JSON form and SHA-256 truncation here are re-implemented for
 * the tests (different code path, array-based serialization) so expected
 * ETags are computed independently of src/core/hash.ts. Concrete values were
 * also pre-computed once with a standalone Node one-liner and frozen below;
 * the tests assert against the frozen strings, guarding the oracle itself.
 */
import { createHash } from 'node:crypto';

/** Independent canonicalizer: JSON arrays with sorted object keys. */
export function independentCanonical(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return `[${value.map(independentCanonical).join(',')}]`;
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([k, v]) => `${JSON.stringify(k)}:${independentCanonical(v)}`);
    return `{${entries.join(',')}}`;
  }
  return JSON.stringify(value);
}

export function independentETag(version: number, body: unknown): string {
  const hash = createHash('sha256').update(independentCanonical(body)).digest('hex').slice(0, 8);
  return `"v${version}-${hash}"`;
}

/** Frozen values from the offline pre-computation (see tests/fixtures/http-cases.json). */
export const FROZEN_ETAGS = {
  widgetV1: '"v1-d00ddb0d"',
  widgetV2Count2: '"v2-ce5ede1f"',
  widgetV3AfterMerge: '"v3-2a87e499"'
} as const;
