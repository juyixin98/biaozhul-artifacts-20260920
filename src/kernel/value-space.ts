import { asSchema, type JsonObject, type JsonValue, type RawSchema } from '../contract/types.js';

/**
 * Synthesize concrete witness VALUES from schemas ("value space" sampling).
 *
 * The kernel needs actual minimal examples, not just "field changed":
 *   - sample(schema)         -> a value accepted by the schema
 *   - sampleOutside(schema)  -> a minimal value the schema rejects, when one
 *                               is constructible from supported keywords
 *
 * Ref traversal is bounded: a per-document stack of pointers and a hard depth
 * cap turn cyclic schemas into cycle leaves instead of infinite recursion.
 */

import type { SchemaResolver } from './types.js';

const MAX_SCHEMA_DEPTH = 16;

/**
 * Marker witness instruction: "a valid instance with this leaf OMITTED".
 * Required-property changes are witnessed by omission, not by a value.
 * It is a plain JSON sentinel so it survives serialization; injectLeaf never
 * leaves it inside the produced instance.
 */
export const OMIT: { readonly __witness_omit__: true } = { __witness_omit__: true };
export type WitnessInstruction = JsonValue | typeof OMIT;

export function isOmit(v: JsonValue): boolean {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
    && (v as JsonObject)['__witness_omit__'] === true;
}

interface TraverseCtx {
  resolver: SchemaResolver;
  stack: string[];
  depth: number;
}

function step(ctx: TraverseCtx, ref?: string): TraverseCtx {
  return {
    resolver: ctx.resolver,
    stack: ref ? [...ctx.stack, ref] : ctx.stack,
    depth: ctx.depth + 1,
  };
}

function asObj(schema: JsonValue | undefined): JsonObject | undefined {
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return undefined;
  return schema;
}

/** Follow a leading $ref (3.1 allows siblings; we honor $ref like 3.0 subset). */
export function deref(schema: RawSchema | undefined, ctx: TraverseCtx): RawSchema | undefined {
  const obj = asObj(schema);
  if (!obj || typeof obj['$ref'] !== 'string') return schema;
  if (ctx.depth >= MAX_SCHEMA_DEPTH) return undefined;
  const res = ctx.resolver.resolve(obj['$ref'], ctx.stack);
  if (!res.node) return undefined; // missing/cycle/external: caller reports it
  return deref(res.node, step(ctx, obj['$ref']));
}

export function newCtx(resolver: SchemaResolver): TraverseCtx {
  return { resolver, stack: [], depth: 0 };
}

export function effectiveTypes(schema: RawSchema | undefined, ctx: TraverseCtx): string[] {
  const obj = asObj(deref(schema, ctx));
  if (!obj) return [];
  return typesOf(obj);
}

function typesOf(obj: JsonObject): string[] {
  const t = obj['type'];
  if (typeof t === 'string') return [t];
  if (Array.isArray(t)) return t.filter((x): x is string => typeof x === 'string');
  return [];
}

/** A value accepted by the schema, preferring enum/const/default, minimally shaped. */
export function sample(schema: RawSchema | undefined, ctx: TraverseCtx): JsonValue | undefined {
  if (ctx.depth >= MAX_SCHEMA_DEPTH) return null; // cycle leaf: neutral value
  const obj = asObj(deref(schema, ctx));
  if (!obj) return undefined; // unconstrained -> caller decides

  if (obj['const'] !== undefined) return obj['const'];
  if (Array.isArray(obj['enum']) && obj['enum'].length > 0) return obj['enum'][0] as JsonValue;
  if (obj['default'] !== undefined) return obj['default'];

  const types = typesOf(obj);
  const pick = (want: string): JsonValue | undefined => {
    switch (want) {
      case 'string':
        return 'x';
      case 'integer':
        return 0;
      case 'number':
        return 0;
      case 'boolean':
        return true;
      case 'null':
        return null;
      case 'array': {
        const item = sample(asSchema(obj['items']), step(ctx));
        return [item ?? null];
      }
      case 'object': {
        const props = asObj(obj['properties']);
        const required = Array.isArray(obj['required'])
          ? (obj['required'] as JsonValue[]).filter((x): x is string => typeof x === 'string')
          : [];
        const out: JsonObject = {};
        for (const key of required) {
          const child = props ? asSchema(props[key]) : undefined;
          const v = sample(child, step(ctx));
          if (v !== undefined) out[key] = v;
        }
        return out;
      }
      default:
        return undefined;
    }
  };

  for (const t of types) {
    const v = pick(t);
    if (v !== undefined) return v;
  }
  return undefined;
}

/**
 * A value REJECTED by the schema along a specific supported dimension.
 * Returns undefined when no rejection witness can be built from known
 * keywords (the kernel then emits an `undetermined` finding instead).
 */
export function sampleRejection(
  schema: RawSchema | undefined,
  ctx: TraverseCtx,
  dimension: 'type' | 'enum' | 'nullability' | 'required-prop' | 'extra-prop',
): JsonValue | undefined {
  if (ctx.depth >= MAX_SCHEMA_DEPTH) return undefined;
  const obj = asObj(deref(schema, ctx));
  if (!obj) return undefined;

  switch (dimension) {
    case 'type': {
      const types = typesOf(obj);
      const outside: Record<string, JsonValue> = {
        string: 0,
        integer: 'x',
        number: 'x',
        boolean: 'x',
        object: 'x',
        array: 'x',
        null: 'x',
      };
      for (const t of Object.keys(outside)) {
        if (!types.includes(t)) return outside[t];
      }
      return undefined;
    }
    case 'enum': {
      if (!Array.isArray(obj['enum']) || obj['enum'].length === 0) return undefined;
      // Pick a value of the same JSON type that is not in the enum.
      const first = obj['enum'][0];
      const members = obj['enum'] as JsonValue[];
      let candidate: JsonValue;
      if (typeof first === 'string') {
        candidate = members.includes('__other__') ? '__different__' : '__other__';
      } else if (typeof first === 'number') {
        candidate = Number.MAX_SAFE_INTEGER;
      } else if (typeof first === 'boolean') {
        candidate = !first;
      } else {
        candidate = '__other__';
      }
      return candidate;
    }
    case 'nullability':
      return null;
    case 'required-prop':
      return undefined; // handled at object level (minimal object missing the prop)
    case 'extra-prop': {
      const props = asObj(obj['properties']);
      let key = 'extra';
      let n = 0;
      while (props && key in props) {
        n += 1;
        key = `extra${n}`;
      }
      return key;
    }
    default:
      return undefined;
  }
}

/** Build a minimal object instance of the schema with one property omitted. */
export function sampleObjectWithoutProp(
  schema: RawSchema | undefined,
  ctx: TraverseCtx,
  omit: string,
): JsonValue | undefined {
  const obj = asObj(deref(schema, ctx));
  if (!obj) return undefined;
  const props = asObj(obj['properties']);
  const required = Array.isArray(obj['required'])
    ? (obj['required'] as JsonValue[]).filter((x): x is string => typeof x === 'string')
    : [];
  const out: JsonObject = {};
  for (const key of required) {
    if (key === omit) continue;
    const child = props ? asSchema(props[key]) : undefined;
    const v = sample(child, step(ctx));
    if (v !== undefined) out[key] = v;
  }
  return out;
}

/**
 * Build a minimally-valid instance of `rootSchema`, then place `raw` at the
 * logical `segments` path ("properties.x.items" style) or omit the final
 * property when `raw` is the OMIT sentinel. This keeps nested-leaf witnesses
 * (e.g. an enum value deep inside a body object) valid everywhere except at
 * the offending leaf, which is what an independent validator expects.
 */
export function injectLeaf(
  rootSchema: RawSchema | undefined,
  ctx: TraverseCtx,
  segments: string[],
  raw: JsonValue,
): JsonValue | undefined {
  const root = sample(rootSchema, ctx);
  return injectSegments(rootSchema, ctx, segments, raw, root);
}

function injectSegments(
  schema: RawSchema | undefined,
  ctx: TraverseCtx,
  segments: string[],
  raw: JsonValue,
  current: JsonValue | undefined,
): JsonValue {
  const [head, ...rest] = segments;
  if (head === undefined) return raw;
  if (ctx.depth >= MAX_SCHEMA_DEPTH) return current ?? null;
  const obj = asObj(deref(schema, ctx));

  if (head === 'items') {
    const itemSchema = obj ? asSchema(obj['items']) : undefined;
    const firstItem = injectSegments(itemSchema, step(ctx), rest, raw, sample(itemSchema, step(ctx)));
    return [firstItem];
  }

  // head is a property name (possibly preceded by a literal 'properties')
  let propName = head;
  let remaining = rest;
  if (head === 'properties') {
    propName = segments[1] as string;
    remaining = segments.slice(2);
  }
  const props = asObj(obj?.['properties']);
  const childSchema = props ? asSchema(props[propName]) : undefined;
  const base: JsonObject = typeof current === 'object' && current !== null && !Array.isArray(current)
    ? { ...(current as JsonObject) }
    : {};

  if (remaining.length === 0) {
    if (isOmit(raw)) {
      delete base[propName];
    } else {
      base[propName] = raw;
    }
    return base;
  }
  const childCurrent = base[propName] ?? sample(childSchema, step(ctx));
  base[propName] = injectSegments(childSchema, step(ctx), remaining, raw, childCurrent);
  return base;
}
