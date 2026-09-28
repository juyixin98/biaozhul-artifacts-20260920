import { createHash } from "node:crypto";

/**
 * Contract layer — entity-tag syntax, comparison and generation.
 *
 * Implements RFC 9110 8.8.3 (entity-tag / OWS-list parsing) and 8.8.3.2
 * (comparison using the weak comparison function), independent of storage.
 */

export interface ParsedETag {
  /** Opaque-tag including quotes, WITHOUT weak prefix: e.g. `"v3-ab12"`. */
  readonly tag: string;
  /** True when the wire form carried the "W/" weak prefix. */
  readonly weak: boolean;
}

export class ETagSyntaxError extends Error {
  override readonly name = "ETagSyntaxError";
}

const OPAQUE_TAG_PATTERN = /^"[\x21\x23-\x7e\x80-\xff]*"$/;

/**
 * Parse a single entity-tag. Surrounding OWS is tolerated; inner whitespace
 * is not (it would imply a malformed list).
 */
export function parseETag(raw: string): ParsedETag {
  const value = raw.trim();
  let weak = false;
  let tag = value;
  if (value.startsWith("W/") || value.startsWith("w/")) {
    weak = true;
    tag = value.slice(2);
  }
  if (!OPAQUE_TAG_PATTERN.test(tag)) {
    throw new ETagSyntaxError(`Malformed entity-tag: ${JSON.stringify(raw)}`);
  }
  return { tag, weak };
}

/** Split an If-Match / If-None-Match header into individual entity-tags or "*". */
export function parseETagList(header: string): { tags: ParsedETag[]; star: boolean } {
  const tags: ParsedETag[] = [];
  let star = false;
  const elements = splitCommaList(header);
  if (elements.length === 0) {
    throw new ETagSyntaxError("Empty entity-tag list");
  }
  for (const element of elements) {
    if (element === "*") {
      star = true;
      continue;
    }
    tags.push(parseETag(element));
  }
  if (star && tags.length > 0) {
    // RFC 9110: "*" is a complete field value on its own; mixing is invalid.
    throw new ETagSyntaxError(`If-Match/If-None-Match mixes "*" with entity-tags: ${JSON.stringify(header)}`);
  }
  return { tags, star };
}

/**
 * Split an OWS-delimited comma list while protecting quoted strings: commas
 * inside an opaque tag are... actually not legal (opaque tags exclude ","),
 * but we still scan quote state to reject stray delimiters precisely.
 */
function splitCommaList(header: string): string[] {
  const parts: string[] = [];
  let current = "";
  let inQuotes = false;
  for (const ch of header.trim()) {
    if (ch === '"') inQuotes = !inQuotes;
    if (ch === "," && !inQuotes) {
      if (current.trim() !== "") parts.push(current.trim());
      current = "";
    } else {
      current += ch;
    }
  }
  if (current.trim() !== "") parts.push(current.trim());
  return parts;
}

/**
 * Weak comparison (RFC 9110 8.8.3.2): two tags match when their opaque-tags
 * are equal character-for-character, regardless of weakness indicators.
 */
export function weakCompare(a: ParsedETag, b: ParsedETag): boolean {
  return a.tag === b.tag;
}

/**
 * Strong comparison: both validators must be strong AND opaque-tags equal.
 * Used by If-Match when the method demands byte-equivalent representations
 * (we apply it for PUT/DELETE — see method policy).
 */
export function strongCompare(a: ParsedETag, b: ParsedETag): boolean {
  return !a.weak && !b.weak && a.tag === b.tag;
}

/** Does the list contain a tag matching `current` under the chosen function? */
export function listMatches(
  list: { tags: ParsedETag[]; star: boolean },
  current: ParsedETag,
  mode: "strong" | "weak",
): boolean {
  const compare = mode === "strong" ? strongCompare : weakCompare;
  return list.tags.some((candidate) => compare(candidate, current));
}

/**
 * Build a strong opaque validator for a committed snapshot.
 * Tag = "v<version>-<12 hex chars of sha-256(body)>".
 * The body digest binds the validator to the exact representation bytes;
 * the version makes collision/debug correlation trivial.
 *
 * Synchronous on purpose: it must run inside the storage transaction that
 * performs condition evaluation and the write atomically.
 */
export function buildStrongETag(version: number, body: string): string {
  const digest = sha256Hex(body);
  return `"v${version}-${digest.slice(0, 12)}"`;
}

/** Wire form honoring the configured emission strength. */
export function renderETag(strongTag: string, strength: "strong" | "weak"): string {
  return strength === "weak" ? `W/${strongTag}` : strongTag;
}

/** Strip a possible W/ prefix for storage/recording of the canonical strong tag. */
export function canonicalTag(raw: string): string {
  return raw.startsWith("W/") || raw.startsWith("w/") ? raw.slice(2) : raw;
}

export function sha256Hex(input: string): string {
  return createHash("sha256").update(input, "utf8").digest("hex");
}
