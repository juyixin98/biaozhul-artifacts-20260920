/**
 * Entity-tag parsing and comparison (RFC 9110 §8.8.1 / §8.8.3).
 *
 * An entity-tag is either:
 *   W/"<opaque>"  a weak validator
 *   "<opaque>"    a strong validator
 *   *             wildcard (only valid inside If-Match / If-None-Match lists)
 *
 * The opaque-tag content is treated as an atomic string: comparisons never
 * interpret its contents, and weak vs strong only differs by the W/ prefix.
 */

export interface EntityTag {
  readonly opaque: string;
  readonly weak: boolean;
}

export interface IfList {
  /** true when the header value is the single wildcard "*". */
  readonly wildcard: boolean;
  /** Parsed tags; empty when wildcard is true. */
  readonly tags: readonly EntityTag[];
}

const OPAQUE_CHAR = /^[\x21\x23-\x7e\x80-\xff]*$/;

/**
 * Parse a single entity-tag such as `W/"v3"` or `"abc"`.
 * Returns null when the text is not a syntactically valid entity-tag.
 */
export function parseEntityTag(input: string): EntityTag | null {
  let s = input;
  let weak = false;
  if (s.startsWith('W/') || s.startsWith('w/')) {
    weak = true;
    s = s.slice(2);
  }
  if (s.length < 2 || s[0] !== '"' || s[s.length - 1] !== '"') return null;
  const opaque = s.slice(1, -1);
  // Empty opaque tags ("") are legal, but our generator never emits them;
  // accept them for interoperability as long as the charset is valid.
  if (!OPAQUE_CHAR.test(opaque)) return null;
  return { opaque, weak };
}

/**
 * Parse an If-Match / If-None-Match header value: a comma-separated list of
 * entity-tags or the single wildcard "*". Returns null on any syntax error,
 * including a wildcard mixed with tags (e.g. `*, "x"`).
 */
export function parseIfList(raw: string): IfList | null {
  const trimmed = raw.trim();
  if (trimmed === '*') return { wildcard: true, tags: [] };
  if (trimmed === '') return null;

  const tags: EntityTag[] = [];
  // Split only on commas that sit outside the quoted-string: opaque tags may
  // legally contain commas, so naive split(',') would corrupt them.
  let depth = 0;
  let segmentStart = 0;
  for (let i = 0; i < trimmed.length; i++) {
    const ch = trimmed[i];
    if (ch === '"') depth = depth === 0 ? 1 : 0;
    if (ch === ',' && depth === 0) {
      const part = trimmed.slice(segmentStart, i).trim();
      if (part === '*') return null; // wildcard cannot appear in a list
      const tag = parseEntityTag(part);
      if (!tag) return null;
      tags.push(tag);
      segmentStart = i + 1;
    }
  }
  const last = trimmed.slice(segmentStart).trim();
  if (last === '*') return null;
  const lastTag = parseEntityTag(last);
  if (!lastTag) return null;
  tags.push(lastTag);
  return { wildcard: false, tags };
}

/** Format an entity-tag for use in an ETag response header. */
export function formatETag(opaque: string, weak: boolean): string {
  return `${weak ? 'W/' : ''}"${opaque}"`;
}

/**
 * Strong comparison (RFC 9110 §8.8.3.2): true only when BOTH validators are
 * strong and their opaque strings are equal. A weak current resource
 * representation can never strongly match anything.
 */
export function strongCompare(current: EntityTag, candidate: EntityTag): boolean {
  return !current.weak && !candidate.weak && current.opaque === candidate.opaque;
}

/**
 * Weak comparison (RFC 9110 §8.8.3.3): true when the opaque strings are equal,
 * regardless of either validator's weakness indicator.
 */
export function weakCompare(current: EntityTag, candidate: EntityTag): boolean {
  return current.opaque === candidate.opaque;
}

/** True if any tag in `list` matches `current` using the given comparison. */
export function listMatches(
  current: EntityTag,
  list: IfList,
  compare: (a: EntityTag, b: EntityTag) => boolean
): boolean {
  if (list.wildcard) return true;
  return list.tags.some((tag) => compare(current, tag));
}
