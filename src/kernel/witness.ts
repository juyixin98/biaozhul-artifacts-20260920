/**
 * Witness sample generation.
 *
 * Produces a minimal concrete JSON value that a producer-side schema allows,
 * preferring declared defaults, then enum literals, then structural samples.
 * The kernel uses these values to *prove* incompatibility: each BREAKING
 * finding carries a value that is valid on one side and rejected on the other.
 */
import type { JsonValue, NormalizedSchema, SchemaType } from '../core/types.js';
import { sampleOfType } from './type-lattice.js';

/**
 * Build a minimal value conforming to `schema` from the producer side.
 * Depth is bounded so recursive structures (reported elsewhere as cycles)
 * can never cause infinite generation.
 */
export function sampleFromSchema(
  schema: NormalizedSchema,
  depth = 0,
): JsonValue {
  const MAX_DEPTH = 6;

  if (schema.hasDefault) return schema.default as JsonValue;
  if (schema.enum !== null && schema.enum.length > 0) return schema.enum[0] as JsonValue;

  const preferred = preferredType(schema.types);

  if (preferred === 'object' || (preferred === undefined && schema.properties.size > 0)) {
    if (depth >= MAX_DEPTH) return {};
    const obj: Record<string, JsonValue> = {};
    for (const [name, prop] of schema.properties) {
      if (prop.hasDefault && prop.default !== undefined) {
        obj[name] = prop.default;
        continue;
      }
      // minimal witness: required fields are enough to be valid
      if (schema.required.has(name)) {
        obj[name] = sampleFromSchema(prop, depth + 1);
      }
    }
    return obj;
  }

  if (preferred === 'array') {
    if (depth >= MAX_DEPTH) return [];
    return schema.items ? [sampleFromSchema(schema.items, depth + 1)] : [];
  }

  if (preferred) return sampleOfType(preferred);
  return {};
}

/**
 * Choose the most useful representative type. Prefers concrete primitives and
 * `null` last, because witnesses like `null` are produced explicitly when
 * nullability is the dimension under test.
 */
function preferredType(types: readonly SchemaType[]): SchemaType | undefined {
  if (types.length === 0) return undefined;
  const order: SchemaType[] = [
    'object', 'array', 'string', 'integer', 'number', 'boolean', 'null',
  ];
  for (const t of order) if (types.includes(t)) return t;
  return types[0];
}

/**
 * Pick the first producer enum literal that the consumer does not allow.
 * Returns null when every producer literal is accepted (no witness).
 */
export function firstRejectedEnumLiteral(
  producer: NormalizedSchema,
  isAccepted: (v: JsonValue) => boolean,
): JsonValue | null {
  if (!producer.enum) return null;
  for (const literal of producer.enum) {
    if (!isAccepted(literal)) return literal;
  }
  return null;
}
