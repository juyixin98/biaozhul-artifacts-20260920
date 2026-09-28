/**
 * Type lattice for the supported subset.
 *
 * Compatibility question answered here:
 *   "Is every value of the PRODUCER side accepted by the CONSUMER side?"
 * The kernel swaps which contract is producer depending on direction:
 *   request  -> producer = OLD contract (old client emits), consumer = NEW
 *   response -> producer = NEW contract (new server emits), consumer = OLD
 */
import type { JsonValue, NormalizedSchema, SchemaType } from '../core/types.js';

/**
 * Whether a value of type `from` is guaranteed acceptable where `to` is
 * declared. `integer` is a subtype of `number`; `null` only matches `null`.
 */
export function isTypeAcceptable(from: SchemaType, to: SchemaType): boolean {
  if (from === to) return true;
  if (from === 'integer' && to === 'number') return true;
  return false;
}

/**
 * Find the first producer-side type that no consumer-side type accepts.
 * Empty type lists mean "unconstrained" and accept everything.
 */
export function findRejectedType(
  producerTypes: readonly SchemaType[],
  consumerTypes: readonly SchemaType[],
): SchemaType | null {
  if (producerTypes.length === 0 || consumerTypes.length === 0) return null;
  for (const t of producerTypes) {
    if (!consumerTypes.some((c) => isTypeAcceptable(t, c))) return t;
  }
  return null;
}

/** Minimal concrete sample value of a given type (used to build witnesses). */
export function sampleOfType(type: SchemaType): JsonValue {
  switch (type) {
    case 'null':
      return null;
    case 'boolean':
      return true;
    case 'integer':
      return 1;
    case 'number':
      return 1.5;
    case 'string':
      return 'string';
    case 'array':
      return [];
    case 'object':
      return {};
  }
}

/** Deep, JSON-only equality for enum/default comparison. */
export function jsonEqual(a: JsonValue | undefined, b: JsonValue | undefined): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a === null || b === null) return a === b;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((v, i) => jsonEqual(v, b[i]));
  }
  if (typeof a === 'object' && typeof b === 'object') {
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    return ka.every((k) => jsonEqual(a[k], b[k]));
  }
  return false;
}

/**
 * Is `value` accepted by a consumer schema's type+enum surface?
 * Used to select a concrete producer enum literal that the consumer rejects.
 */
export function isValueAcceptedBy(value: JsonValue, consumer: NormalizedSchema): boolean {
  if (consumer.types.length > 0 && !matchesAnyType(value, consumer.types)) return false;
  if (consumer.enum !== null && !consumer.enum.some((e) => jsonEqual(e, value))) return false;
  return true;
}

function matchesAnyType(value: JsonValue, types: readonly SchemaType[]): boolean {
  return types.some((t) => matchesType(value, t));
}

function matchesType(value: JsonValue, t: SchemaType): boolean {
  switch (t) {
    case 'null':
      return value === null;
    case 'boolean':
      return typeof value === 'boolean';
    case 'string':
      return typeof value === 'string';
    case 'integer':
      return typeof value === 'number' && Number.isInteger(value);
    case 'number':
      return typeof value === 'number';
    case 'array':
      return Array.isArray(value);
    case 'object':
      return typeof value === 'object' && value !== null && !Array.isArray(value);
  }
}
