import type { JsonObject, JsonValue, RawSchema } from './types.js';
import { ContractParseError } from './loader.js';

/**
 * Bounded $ref resolver.
 *
 * - Only local refs are supported (`#/components/schemas/Foo`).
 * - Cycles are *bounded*: following a ref that is already on the current
 *   resolution stack returns a cycle marker instead of looping forever.
 * - The kernel traverses schemas via the resolver, so recursive structures
 *   (e.g. a tree node referencing itself) are compared up to MAX_DEPTH.
 */

export const MAX_REF_DEPTH = 32;

export interface Resolution {
  /** The target schema object, or null when `missing` / `cycle` is set. */
  node: RawSchema | null;
  /** JSON-pointer path of the target within the document. */
  pointer: string;
  missing?: true;
  cycle?: true;
  external?: true;
}

function unescapeToken(token: string): string {
  return token.replace(/~1/g, '/').replace(/~0/g, '~');
}

export function resolvePointer(doc: JsonValue, pointer: string): JsonValue | undefined {
  if (pointer === '' || pointer === '#') return doc;
  let p = pointer;
  if (p.startsWith('#')) p = p.slice(1);
  if (!p.startsWith('/')) return undefined;
  let cur: JsonValue = doc;
  for (const rawToken of p.split('/').slice(1)) {
    const token = unescapeToken(rawToken);
    if (Array.isArray(cur)) {
      const idx = Number(token);
      if (!Number.isInteger(idx) || idx < 0 || idx >= cur.length) return undefined;
      cur = cur[idx] as JsonValue;
    } else if (typeof cur === 'object' && cur !== null) {
      if (!Object.prototype.hasOwnProperty.call(cur, token)) return undefined;
      cur = (cur as JsonObject)[token] as JsonValue;
    } else {
      return undefined;
    }
  }
  return cur;
}

/**
 * Resolve a $ref string relative to the document, tracking the chain of
 * pointers already visited on this resolution path.
 */
export function resolveRef(
  doc: JsonValue,
  ref: string,
  stack: readonly string[] = [],
): Resolution {
  if (!ref.startsWith('#')) {
    return { node: null, pointer: ref, external: true };
  }
  const pointer = ref.slice(1);
  if (stack.includes(pointer)) {
    return { node: null, pointer, cycle: true };
  }
  if (stack.length >= MAX_REF_DEPTH) {
    return { node: null, pointer, cycle: true };
  }
  const target = resolvePointer(doc, ref);
  if (target === undefined || typeof target !== 'object') {
    return { node: null, pointer, missing: true };
  }
  return { node: target as RawSchema, pointer };
}

/** Assert a raw schema node is an object (boolean schemas handled by callers). */
export function schemaObject(schema: RawSchema | undefined): JsonObject | undefined {
  if (schema === undefined) return undefined;
  if (schema === true || schema ===false) return undefined;
  return schema;
}

export function ensureLocalRefs(doc: JsonValue, seen: Set<string> = new Set()): string[] {
  /** Return JSON pointers of every $ref that cannot be resolved locally. */
  const bad: string[] = [];
  const walk = (node: JsonValue, pointer: string, depth: number): void => {
    if (depth > MAX_REF_DEPTH) return;
    if (Array.isArray(node)) {
      node.forEach((child, i) => walk(child, `${pointer}/${i}`, depth + 1));
      return;
    }
    if (typeof node !== 'object' || node === null) return;
    const obj = node as JsonObject;
    const ref = obj['$ref'];
    if (typeof ref === 'string') {
      const res = resolveRef(doc, ref);
      if (res.missing) bad.push(`${pointer} -> ${ref} (unresolved)`);
      if (res.external) bad.push(`${pointer} -> ${ref} (external refs unsupported)`);
      const key = `${pointer}=>${ref}`;
      if (!seen.has(key) && res.node) {
        seen.add(key);
        walk(res.node, res.pointer, depth + 1);
      }
    }
    for (const [k, v] of Object.entries(obj)) {
      if (k === '$ref') continue;
      walk(v, `${pointer}/${k.replace(/~/g, '~0').replace(/\//g, '~1')}`, depth + 1);
    }
  };
  walk(doc, '', 0);
  return bad;
}

export { ContractParseError };
