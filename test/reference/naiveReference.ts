/**
 * Independent reference implementation of RFC 6902 (restricted to
 * test/add/remove/replace/move/copy).
 *
 * Independence from the kernel under test (src/patch.ts):
 * - the kernel builds NEW immutable structures sharing unchanged branches;
 *   this reference deep-clones the input and performs IN-PLACE mutation;
 * - the kernel walks tokens with index loops; this one uses recursive descent
 *   with a positional cursor;
 * - escapes are undone with a regex callback instead of a character loop;
 * - failures carry local RefError tags that are translated to the public
 *   category vocabulary through REF_CATEGORY (an explicit mapping is itself a
 *   cross-check that the two implementations classify edge cases identically).
 *
 * It is intentionally small and literal w.r.t. RFC 6902 §4.
 */

export type RefJson =
  | null
  | boolean
  | number
  | string
  | RefJson[]
  | { [k: string]: RefJson };

type RefTag =
  | 'bad-escape'
  | 'bad-pointer'
  | 'not-found'
  | 'parent-missing'
  | 'bad-index'
  | 'index-oob'
  | 'type-mismatch'
  | 'test-mismatch'
  | 'move-descendant'
  | 'remove-root';

export const REF_CATEGORY: Record<RefTag, string> = {
  'bad-escape': 'INVALID_POINTER',
  'bad-pointer': 'INVALID_POINTER',
  'not-found': 'POINTER_TARGET_MISSING',
  'parent-missing': 'POINTER_PARENT_MISSING',
  'bad-index': 'ARRAY_INDEX_INVALID',
  'index-oob': 'ARRAY_INDEX_OUT_OF_BOUNDS',
  'type-mismatch': 'PATH_TYPE_MISMATCH',
  'test-mismatch': 'TEST_FAILURE',
  'move-descendant': 'MOVE_INTO_DESCENDANT',
  'remove-root': 'PATH_TYPE_MISMATCH',
};

class RefError extends Error {
  constructor(
    readonly tag: RefTag,
    readonly at: number,
  ) {
    super(tag);
  }
}

interface RefStep {
  index: number;
  op: string;
}

export interface RefResult {
  ok: boolean;
  result: RefJson;
  category: string | null;
  failedAtIndex: number;
  applied: number;
  steps: RefStep[];
}

function decodeToken(rawToken: string): string {
  return rawToken.replace(/~(.)/g, (_match, ch: string) => {
    if (ch === '0') return '~';
    if (ch === '1') return '/';
    throw new RefError('bad-escape', -1);
  });
}

function splitPointer(pointer: unknown): string[] {
  if (typeof pointer !== 'string') throw new RefError('bad-pointer', -1);
  if (pointer === '') return [];
  if (pointer[0] !== '/') throw new RefError('bad-pointer', -1);
  const pieces = pointer.split('/').slice(1);
  return pieces.map(decodeToken);
}

function indexOf(token: string): number | 'append' {
  if (token === '-') return 'append';
  if (!/^\d+$/.test(token)) throw new RefError('bad-index', -1);
  if (token.length > 1 && token[0] === '0') throw new RefError('bad-index', -1);
  const n = Number(token);
  if (!Number.isSafeInteger(n)) throw new RefError('bad-index', -1);
  return n;
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a === 'number' && typeof b === 'number') return Number.isNaN(a) && Number.isNaN(b);
  if (a === null || b === null || typeof a !== typeof b) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    return a.every((v, i) => deepEqual(v, b[i]));
  }
  if (typeof a === 'object' && typeof b === 'object') {
    const ao = a as Record<string, unknown>;
    const bo = b as Record<string, unknown>;
    const ka = Object.keys(ao);
    if (ka.length !== Object.keys(bo).length) return false;
    return ka.every((k) => Object.prototype.hasOwnProperty.call(bo, k) && deepEqual(ao[k], bo[k]));
  }
  return false;
}

function getAt(doc: RefJson, tokens: string[], opIndex: number): RefJson {
  let cur: RefJson = doc;
  for (let depth = 0; depth < tokens.length; depth += 1) {
    const token = tokens[depth]!;
    const isLeaf = depth === tokens.length - 1;
    if (Array.isArray(cur)) {
      const i = indexOf(token);
      if (i === 'append' || i >= cur.length) {
        throw new RefError(isLeaf ? 'not-found' : 'parent-missing', opIndex);
      }
      cur = cur[i]!;
    } else if (cur !== null && typeof cur === 'object') {
      if (!Object.prototype.hasOwnProperty.call(cur, token)) {
        throw new RefError(isLeaf ? 'not-found' : 'parent-missing', opIndex);
      }
      cur = cur[token]!;
    } else {
      throw new RefError('type-mismatch', opIndex);
    }
  }
  return cur;
}

function parentOf(
  doc: RefJson,
  tokens: string[],
  opIndex: number,
): { container: RefJson[] | Record<string, RefJson>; last: string } {
  let cur: RefJson = doc;
  for (let i = 0; i < tokens.length - 1; i += 1) {
    const token = tokens[i]!;
    if (Array.isArray(cur)) {
      const idx = indexOf(token);
      if (idx === 'append' || idx >= cur.length) throw new RefError('parent-missing', opIndex);
      cur = cur[idx]!;
    } else if (cur !== null && typeof cur === 'object') {
      if (!Object.prototype.hasOwnProperty.call(cur, token)) {
        throw new RefError('parent-missing', opIndex);
      }
      cur = cur[token]!;
    } else {
      throw new RefError('type-mismatch', opIndex);
    }
  }
  const last = tokens[tokens.length - 1]!;
  if (Array.isArray(cur)) return { container: cur, last };
  if (cur !== null && typeof cur === 'object') {
    return { container: cur as Record<string, RefJson>, last };
  }
  throw new RefError('type-mismatch', opIndex);
}

function doAdd(doc: RefJson, tokens: string[], value: RefJson, opIndex: number): RefJson {
  if (tokens.length === 0) return value;
  const { container, last } = parentOf(doc, tokens, opIndex);
  if (Array.isArray(container)) {
    const i = indexOf(last);
    if (i === 'append') {
      container.push(value);
    } else {
      if (i > container.length) throw new RefError('index-oob', opIndex);
      container.splice(i, 0, value);
    }
  } else {
    container[last] = value;
  }
  return doc;
}

function doRemove(doc: RefJson, tokens: string[], opIndex: number): RefJson {
  if (tokens.length === 0) throw new RefError('remove-root', opIndex);
  // Existence is checked first (RFC: target must exist).
  getAt(doc, tokens, opIndex);
  const { container, last } = parentOf(doc, tokens, opIndex);
  if (Array.isArray(container)) {
    const i = indexOf(last);
    if (i === 'append' || i >= container.length) throw new RefError('index-oob', opIndex);
    container.splice(i, 1);
  } else {
    delete container[last];
  }
  return doc;
}

function doReplace(doc: RefJson, tokens: string[], value: RefJson, opIndex: number): RefJson {
  if (tokens.length === 0) return value;
  getAt(doc, tokens, opIndex);
  const { container, last } = parentOf(doc, tokens, opIndex);
  if (Array.isArray(container)) {
    const i = indexOf(last);
    if (i === 'append' || i >= container.length) throw new RefError('index-oob', opIndex);
    container[i] = value;
  } else {
    container[last] = value;
  }
  return doc;
}

function isPrefixOf(shorter: string[], longer: string[]): boolean {
  return shorter.length < longer.length && shorter.every((t, i) => t === longer[i]);
}

interface RawOp {
  op?: unknown;
  path?: unknown;
  value?: unknown;
  from?: unknown;
}

/**
 * Apply a raw patch. Contract validation is intentionally light here: the
 * differential harness feeds well-formed operation objects; pointer and
 * evaluation errors are the focus. Returns the cloned result only when every
 * operation succeeds — the input itself is never touched.
 */
export function referenceApply(input: RefJson, rawOps: unknown): RefResult {
  const original = input;
  let doc: RefJson = structuredClone(input);
  const steps: RefStep[] = [];

  if (!Array.isArray(rawOps)) {
    return failResult(original, 'MALFORMED_PATCH', -1, steps);
  }

  for (let i = 0; i < rawOps.length; i += 1) {
    const entry = rawOps[i] as RawOp;
    try {
      const op = typeof entry?.op === 'string' ? entry.op : null;
      const path = splitPointer(entry?.path);
      switch (op) {
        case 'test': {
          const actual = getAt(doc, path, i);
          if (!deepEqual(actual, entry.value)) throw new RefError('test-mismatch', i);
          break;
        }
        case 'add':
          // Root add returns a brand-new root; rebind the local reference.
          doc = doAdd(doc, path, entry.value as RefJson, i);
          break;
        case 'remove':
          doRemove(doc, path, i);
          break;
        case 'replace':
          doc = doReplace(doc, path, entry.value as RefJson, i);
          break;
        case 'move': {
          const from = splitPointer(entry.from);
          if (isPrefixOf(from, path)) throw new RefError('move-descendant', i);
          const value = getAt(doc, from, i);
          doRemove(doc, from, i);
          doc = doAdd(doc, path, value, i);
          break;
        }
        case 'copy': {
          const from = splitPointer(entry.from);
          const value = getAt(doc, from, i);
          doc = doAdd(doc, path, structuredClone(value), i);
          break;
        }
        default:
          return failResult(original, 'MALFORMED_PATCH', i, steps);
      }
      steps.push({ index: i, op: op ?? '?' });
    } catch (err) {
      if (err instanceof RefError) {
        return failResult(original, REF_CATEGORY[err.tag], err.at === -1 ? i : err.at, steps);
      }
      return failResult(original, 'MALFORMED_PATCH', i, steps);
    }
  }

  return { ok: true, result: doc, category: null, failedAtIndex: -1, applied: rawOps.length, steps };
}

function failResult(
  original: RefJson,
  category: string,
  failedAtIndex: number,
  steps: RefStep[],
): RefResult {
  return { ok: false, result: original, category, failedAtIndex, applied: steps.length, steps };
}
