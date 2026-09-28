/**
 * JSON Pointer (RFC 6901) support for the patch kernel.
 *
 * A pointer is a string of `/`-separated reference tokens; within a token
 * `~1` unescapes to `/` and `~0` unescapes to `~` (in that order).
 *
 * This module deliberately performs no mutation: it only parses pointers and
 * reads structures. The kernel owns every structural change.
 */

import { PatchError } from './errors';
import type { JsonValue } from './equality';

/** Empty string is a valid pointer: the whole document. */
export interface JsonPointer {
  readonly raw: string;
  readonly tokens: readonly string[];
}

const ROOT: JsonPointer = { raw: '', tokens: Object.freeze([]) };

/** Parse and validate a JSON Pointer string into reference tokens. */
export function parsePointer(raw: unknown): JsonPointer {
  if (typeof raw !== 'string') {
    throw new PatchError(
      'INVALID_POINTER',
      'JSON Pointer must be a string',
      null,
      { receivedType: typeof raw },
    );
  }
  if (raw === '') return ROOT;
  if (!raw.startsWith('/')) {
    throw new PatchError(
      'INVALID_POINTER',
      'Non-empty JSON Pointer must start with "/"',
      null,
      { pointer: raw },
    );
  }
  // Every byte is legal in a token; only the escape sequences carry meaning.
  const tokens = raw
    .split('/')
    .slice(1)
    .map((token) => unescapeToken(token, raw));
  return { raw, tokens: Object.freeze(tokens) };
}

function unescapeToken(token: string, rawPointer: string): string {
  if (!token.includes('~')) return token;
  let out = '';
  for (let i = 0; i < token.length; i += 1) {
    const ch = token[i];
    if (ch !== '~') {
      out += ch;
      continue;
    }
    const next = token[i + 1];
    if (next === '0') {
      out += '~';
      i += 1;
    } else if (next === '1') {
      out += '/';
      i += 1;
    } else {
      throw new PatchError(
        'INVALID_POINTER',
        'Invalid escape sequence in JSON Pointer token: "~" must be followed by "0" or "1"',
        null,
        { pointer: rawPointer, token },
      );
    }
  }
  return out;
}

/** Re-escape a reference token for display/round-tripping. */
export function escapeToken(token: string): string {
  return token.replace(/~/g, '~0').replace(/\//g, '~1');
}

/**
 * Array index rules from RFC 6902 §4:
 * - tokens must consist of digits only
 * - leading zero is forbidden except the single token "0"
 * - "-" means "one past the end" (append) and is only legal for add
 *
 * Returns the resolved numeric index, or -1 for the append token "-".
 */
export function parseArrayIndex(token: string): number {
  if (token === '-') return -1;
  if (!/^[0-9]+$/.test(token)) {
    throw new PatchError(
      'ARRAY_INDEX_INVALID',
      'Array reference token must be an index of digits or "-"',
      null,
      { token },
    );
  }
  if (token.length > 1 && token.startsWith('0')) {
    throw new PatchError(
      'ARRAY_INDEX_INVALID',
      'Array index must not have a leading zero',
      null,
      { token },
    );
  }
  const n = Number(token);
  if (!Number.isSafeInteger(n)) {
    throw new PatchError(
      'ARRAY_INDEX_INVALID',
      'Array index is not a safe integer',
      null,
      { token },
    );
  }
  return n;
}

/**
 * Resolve a pointer to its referenced value. Throws when any part is absent.
 * Failure category is depth-aware: a missing/!traversable token on the LAST
 * reference token is a missing target, earlier tokens are a missing parent
 * chain. This matches the RFC intuition used by remove/replace (target must
 * exist) and keeps classification stable across implementations.
 */
export function resolve(doc: JsonValue, pointer: JsonPointer): JsonValue {
  let cur: JsonValue = doc;
  const last = pointer.tokens.length - 1;
  for (let depth = 0; depth <= last; depth += 1) {
    const token = pointer.tokens[depth]!;
    const isLeaf = depth === last;
    cur = stepInto(cur, token, pointer.raw, isLeaf);
  }
  return cur;
}

function stepInto(
  cur: JsonValue,
  token: string,
  rawPointer: string,
  isLeaf: boolean,
): JsonValue {
  const missingCategory = isLeaf ? 'POINTER_TARGET_MISSING' : 'POINTER_PARENT_MISSING';
  if (Array.isArray(cur)) {
    const idx = parseArrayIndex(token);
    if (idx === -1 || idx >= cur.length) {
      throw new PatchError(
        missingCategory,
        isLeaf
          ? 'Pointer references an array position that does not exist'
          : 'Parent path passes through a non-existent array element',
        null,
        { pointer: rawPointer, index: idx === -1 ? cur.length : idx, length: cur.length },
      );
    }
    return cur[idx]!;
  }
  if (cur !== null && typeof cur === 'object') {
    const obj = cur as Record<string, JsonValue>;
    if (!Object.prototype.hasOwnProperty.call(obj, token)) {
      throw new PatchError(
        missingCategory,
        isLeaf
          ? 'Pointer references an object key that does not exist'
          : 'Parent path passes through a non-existent object key',
        null,
        { pointer: rawPointer, key: token },
      );
    }
    return obj[token]!;
  }
  throw new PatchError(
    'PATH_TYPE_MISMATCH',
    'Pointer traverses a value that is neither an object nor an array',
    null,
    { pointer: rawPointer, key: token, actualType: jsonTypeName(cur) },
  );
}

export function jsonTypeName(value: JsonValue): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}
