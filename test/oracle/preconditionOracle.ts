/**
 * Independent reference oracle for conditional requests.
 *
 * This module deliberately imports NOTHING from src/. It encodes expected
 * RFC 9110 13.2.2 behavior as a hand-written scenario table authored from
 * the specification text, plus a tiny independent model that tracks
 * resource state through a scenario. Tests compare the system under test
 * against these expectations — the expected answers are never produced by
 * the implementation's own evaluator.
 *
 * Precedence encoded (fixed):
 *   1. If-Match present        -> evaluate ONLY If-Match (then date/tag
 *                                 validators below are skipped on success)
 *   2. If-Unmodified-Since     -> fail when last-modified > supplied
 *   3. If-None-Match           -> match: 304 safe / 412 unsafe
 *   4. If-Modified-Since       -> GET/HEAD only, ignored when INM present;
 *                                 304 when last-modified <= supplied
 *
 * Missing resource is a distinct semantic (404 "not-found") from version
 * mismatch (412): e.g. If-Match on an absent resource is 412 per RFC, but
 * a plain GET of an absent resource is 404. The oracle states both.
 */

export type OracleMethod = "GET" | "HEAD" | "PUT" | "DELETE";
export type OracleVerdict = "proceed" | "not-modified" | "precondition-failed" | "malformed" | "not-found";

export interface OracleState {
  readonly exists: boolean;
  readonly version: number;
  readonly etag: string; // canonical strong tag without W/
  readonly updatedAtMs: number;
}

export interface OracleConditions {
  ifMatch?: string;
  ifNoneMatch?: string;
  ifUnmodifiedSince?: string;
  ifModifiedSince?: string;
}

export interface OracleExpectation {
  readonly verdict: OracleVerdict;
  /** Expected HTTP status after method mapping. */
  readonly status: number;
  /** Stable failure category, must equal the implementation's category. */
  readonly category:
    | "none"
    | "malformed-if-match"
    | "malformed-if-none-match"
    | "malformed-date"
    | "if-match-mismatch"
    | "if-none-match-exists"
    | "if-unmodified-since-modified"
    | "not-found";
  readonly rationale: string;
}

/** Minimal independent ETag syntax check (not shared with SUT code). */
function looksLikeTag(token: string): boolean {
  let t = token.trim();
  if (t.slice(0, 2).toUpperCase() === "W/") t = t.slice(2);
  return t.length >= 2 && t.startsWith('"') && t.endsWith('"');
}

function listTokens(header: string): string[] {
  return header
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

function tagEqualOpaque(a: string, b: string): boolean {
  const strip = (t: string) => (t.slice(0, 2).toUpperCase() === "W/" ? t.slice(2) : t);
  return strip(a.trim()) === strip(b.trim());
}

function tagStrongEqual(headerTag: string, current: string): boolean {
  const t = headerTag.trim();
  return !t.toUpperCase().startsWith("W/") && t === current;
}

/** Independent IMF-fixdate parser sufficient for oracle scenarios. */
function parseDate(header: string | undefined): number | null {
  if (!header) return null;
  const ms = Date.parse(header.trim());
  return Number.isNaN(ms) ? null : ms;
}

/**
 * The oracle itself: hand-written decision procedure mirroring RFC 9110
 * 13.2.2. Kept deliberately small and explicit — its purpose is to be an
 * independently readable second implementation.
 */
export function oracleEvaluate(
  method: OracleMethod,
  conds: OracleConditions,
  state: OracleState | null,
): OracleExpectation {
  const safe = method === "GET" || method === "HEAD";
  const present = conds.ifMatch !== undefined || conds.ifNoneMatch !== undefined;
  void present;

  // 1) If-Match
  if (conds.ifMatch !== undefined) {
    const tokens = listTokens(conds.ifMatch);
    const isStar = tokens.length === 1 && tokens[0] === "*";
    if (!isStartList(tokens)) {
      return fail("malformed-if-match", 400, "If-Match is syntactically invalid");
    }
    if (!state) {
      // If-Match on absent representation: 412 (distinct from plain 404).
      return fail("if-match-mismatch", 412, "If-Match on absent resource must fail precondition, not 404");
    }
    if (isStar) {
      return proceed();
    }
    const hit = tokens.some((t) =>
      safe ? tagEqualOpaque(t, state.etag) : tagStrongEqual(t, state.etag),
    );
    return hit
      ? proceed()
      : fail("if-match-mismatch", 412, "If-Match found no matching current validator");
  }

  // 2) If-Unmodified-Since (only evaluated when If-Match absent)
  if (conds.ifUnmodifiedSince !== undefined) {
    const ms = parseDate(conds.ifUnmodifiedSince);
    if (ms === null) {
      return fail("malformed-date", 400, "Invalid HTTP-date in If-Unmodified-Since is rejected");
    }
    if (state && state.updatedAtMs > ms) {
      return fail("if-unmodified-since-modified", 412, "Resource modified after the supplied date");
    }
  }

  // 3) If-None-Match
  if (conds.ifNoneMatch !== undefined) {
    const tokens = listTokens(conds.ifNoneMatch);
    const isStar = tokens.length === 1 && tokens[0] === "*";
    if (!isStartList(tokens)) {
      return fail("malformed-if-none-match", 400, "If-None-Match is syntactically invalid");
    }
    if (state) {
      const matched = isStar || tokens.some((t) => tagEqualOpaque(t, state.etag));
      if (matched) {
        return safe
          ? { verdict: "not-modified", status: 304, category: "none", rationale: "INM match on safe method -> 304" }
          : fail("if-none-match-exists", 412, "INM match on unsafe method -> 412 (e.g. create-vs-existing)");
      }
    }
  }

  // 4) If-Modified-Since (safe methods; ignored when INM present)
  if (safe && conds.ifModifiedSince !== undefined && conds.ifNoneMatch === undefined) {
    const ms = parseDate(conds.ifModifiedSince);
    if (ms !== null && state) {
      if (state.updatedAtMs <= ms) {
        return { verdict: "not-modified", status: 304, category: "none", rationale: "Not modified since supplied date -> 304" };
      }
    }
    // malformed IMS -> ignored, proceeds
  }

  if (!state) {
    return { verdict: "not-found", status: 404, category: "not-found", rationale: "No representation and no failed precondition -> 404" };
  }
  return proceed();
}

function isStartList(tokens: string[]): boolean {
  if (tokens.length === 0) return false;
  if (tokens.length === 1 && tokens[0] === "*") return true;
  if (tokens.includes("*")) return false; // "*" mixed with tags is invalid
  return tokens.every(looksLikeTag);
}

function proceed(): OracleExpectation {
  return { verdict: "proceed", status: 200, category: "none", rationale: "preconditions pass" };
}

function fail(
  category: OracleExpectation["category"],
  status: number,
  rationale: string,
): OracleExpectation {
  const verdict: OracleVerdict =
    status === 400 ? "malformed" : status === 404 ? "not-found" : "precondition-failed";
  return { verdict, status, category, rationale };
}

/**
 * Hand-authored scenario table. Inputs use simple relative offsets against
 * an arbitrary epoch base so dates stay readable and independent of clocks.
 */
export const BASE_MS = Date.UTC(2026, 0, 1, 12, 0, 0); // 2026-01-01T12:00:00Z
export function imf(ms: number): string {
  return new Date(ms).toUTCString();
}
