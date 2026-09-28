/**
 * Contract layer — method policy.
 *
 * Comparison mode is selected by request method per RFC 9110 13.1.1:
 * entity tags in If-Match are compared strongly for unsafe methods; safe
 * methods may use the weak comparison function.
 */

import type { HttpMethod } from "./model.js";

export function isSafeMethod(method: HttpMethod): boolean {
  return method === "GET" || method === "HEAD";
}

/**
 * Comparison function for If-Match:
 * - PUT/DELETE MUST use strong comparison (a weak validator cannot vouch
 *   for byte-identical representations being overwritten).
 * - GET/HEAD use weak comparison.
 */
export function ifMatchComparisonMode(method: HttpMethod): "strong" | "weak" {
  return isSafeMethod(method) ? "weak" : "strong";
}

/** If-None-Match always uses the weak comparison function. */
export const IF_NONE_MATCH_MODE = "weak" as const;
