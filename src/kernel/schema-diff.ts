import { ANNOTATION_KEYWORDS, KNOWN_SCHEMA_KEYWORDS, asSchema, type JsonObject, type JsonValue, type RawSchema } from '../contract/types.js';
import {
  OMIT,
  deref,
  injectLeaf,
  isOmit,
  newCtx,
  sample,
} from './value-space.js';
import type { Direction, Finding, SchemaResolver, Severity } from './types.js';

const MAX_DIFF_DEPTH = 12;

/**
 * Every breaking finding carries a complete wire instance:
 * a minimally-valid instance of the ACCEPTING side's ROOT schema with one
 * leaf replaced (or omitted). The independent test oracle can therefore feed
 * the witness straight to Ajv on both sides.
 */
export interface WitnessInstance {
  /** Schema-relative segments, e.g. ["properties","note"] or ["items"]. */
  segments: string[];
  /** The offending leaf value, or the OMIT sentinel. */
  leaf: JsonValue;
}

interface DiffEnv {
  direction: Direction;
  oldResolver: SchemaResolver;
  newResolver: SchemaResolver;
  /** Root schema of the side that ACCEPTS the witness. */
  acceptRoot: RawSchema | undefined;
  acceptResolver: SchemaResolver;
  add: (f: Omit<Finding, 'id'>) => void;
  /** Wrap a fully-built root schema instance into request/response JSON. */
  wrap: (instance: JsonValue) => JsonValue;
  locAt: (sub: string) => string;
}

interface Frame {
  oldNode: RawSchema | undefined;
  newNode: RawSchema | undefined;
  location: string; // full logical path from root, e.g. "properties.tags.items"
  depth: number;
  seen: Set<string>;
}

function asObj(s: JsonValue | undefined): JsonObject | undefined {
  if (!s || typeof s !== 'object' || Array.isArray(s)) return undefined;
  return s;
}

function sameJson(a: JsonValue | undefined, b: JsonValue | undefined): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function deepEqual(a: JsonValue, b: JsonValue): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function typesOf(obj: JsonObject): string[] {
  const t = obj['type'];
  if (typeof t === 'string') return [t];
  if (Array.isArray(t)) return t.filter((x): x is string => typeof x === 'string');
  return [];
}

/**
 * Compare two schema subtrees for ONE direction.
 *
 * REQUEST:  every OLD-accepted value must remain accepted by NEW.
 * RESPONSE: every NEW-produced value must remain accepted by an OLD client.
 *
 * `wrap` turns a validated root schema instance into the final witness JSON.
 */
export function diffSchemas(
  direction: Direction,
  location: string,
  oldSchema: RawSchema | undefined,
  newSchema: RawSchema | undefined,
  oldResolver: SchemaResolver,
  newResolver: SchemaResolver,
  wrap: (instance: JsonValue) => JsonValue,
  add: (f: Omit<Finding, 'id'>) => void,
  operation: string,
): void {
  const acceptRoot = direction === 'request' ? oldSchema : newSchema;
  const acceptResolver = direction === 'request' ? oldResolver : newResolver;
  const env: DiffEnv = {
    direction,
    oldResolver,
    newResolver,
    acceptRoot,
    acceptResolver,
    add,
    wrap,
    locAt: (sub) => (sub ? `${location}.${sub}` : location),
  };
  walk(env, { oldNode: oldSchema, newNode: newSchema, location: '', depth: 0, seen: new Set() }, operation);
}

/** Build the complete witness instance for a leaf instruction. */
function buildWitness(env: DiffEnv, w: WitnessInstance): JsonValue {
  const ctx = newCtx(env.acceptResolver);
  const instance = injectLeaf(env.acceptRoot, ctx, w.segments, w.leaf);
  return env.wrap(instance ?? null);
}

function baseSegments(frame: Frame, extra?: string): string[] {
  const parts = frame.location ? frame.location.split('.') : [];
  return extra ? [...parts, extra] : parts;
}

function schemaOf(env: DiffEnv, node: RawSchema | undefined, resolver: SchemaResolver): { obj?: JsonObject; refProblem?: 'missing' | 'cycle' | 'external'; ref?: string } {
  const rawObj = asObj(node);
  if (rawObj && typeof rawObj['$ref'] === 'string') {
    const res = resolver.resolve(rawObj['$ref'], []);
    if (res.cycle) return { refProblem: 'cycle', ref: rawObj['$ref'] };
    if (res.missing) return { refProblem: 'missing', ref: rawObj['$ref'] };
    if (res.external) return { refProblem: 'external', ref: rawObj['$ref'] };
  }
  return { obj: asObj(deref(node, newCtx(resolver))) };
}

function walk(env: DiffEnv, frame: Frame, operation: string): void {
  if (frame.depth > MAX_DIFF_DEPTH) {
    env.add(undetermined(env, frame, operation, 'CYCLIC_REF',
      'schema nesting exceeded the bounded depth; deeper differences not evaluated',
      'recursive $ref structure is only compared to a bounded depth'));
    return;
  }

  const oldR = schemaOf(env, frame.oldNode, env.oldResolver);
  const newR = schemaOf(env, frame.newNode, env.newResolver);

  for (const [which, res] of [['old', oldR], ['new', newR]] as const) {
    if (res.refProblem) {
      env.add(undetermined(env, frame, operation,
        res.refProblem === 'cycle' ? 'CYCLIC_REF' : 'UNRESOLVED_REF',
        `${which} schema $ref is ${res.refProblem === 'cycle' ? 'part of a reference cycle (bounded)' : res.refProblem === 'missing' ? 'unresolvable' : 'external (unsupported)'}: ${res.ref ?? ''}`,
        `cannot compare schema because the ${which} side $ref cannot be fully resolved`));
      return;
    }
  }

  const oldObj = oldR.obj;
  const newObj = newR.obj;
  if (!oldObj && !newObj) return;

  // Identical resolved subtrees cannot contain a difference; short-circuiting
  // here also terminates walks over *benign* recursive $ref structures (an
  // unchanged tree node referencing itself) without a CYCLIC_REF finding.
  // Resolved objects are compared so an equal $ref string to a CHANGED target
  // is still walked.
  if (deepEqual(oldObj ?? null, newObj ?? null)) return;

  scanUnknownKeywords(env, frame, oldObj, newObj, operation);

  if (!oldObj || !newObj) {
    env.add(undetermined(env, frame, operation, 'UNKNOWN_KEYWORD',
      env.direction === 'request'
        ? 'NEW introduces a schema where OLD had none; compatibility cannot be fully decided'
        : 'NEW removes a schema OLD clients relied on; compatibility cannot be fully decided',
      'one side is an unconstrained schema and the supported subset cannot derive a minimal witness'));
    return;
  }

  compareConstAndEnum(env, frame, oldObj, newObj, operation);
  compareTypes(env, frame, oldObj, newObj, operation);
  compareDefault(env, frame, oldObj, newObj, operation);
  compareObjectShape(env, frame, oldObj, newObj, operation);
}

function undetermined(
  env: DiffEnv,
  frame: Frame,
  operation: string,
  category: Finding['category'],
  message: string,
  uncertainty: string,
): Omit<Finding, 'id'> {
  return {
    direction: env.direction,
    category,
    severity: 'undetermined',
    operation,
    location: env.locAt(frame.location),
    message,
    oldSide: JSON.stringify(frame.oldNode ?? null),
    newSide: JSON.stringify(frame.newNode ?? null),
    uncertainty,
    witness: {
      description: 'no concrete rejection witness: conclusion is uncertain',
      value: env.wrap(null),
      acceptedBy: 'unknown',
      rejectedBy: 'unknown',
    },
  };
}

function scanUnknownKeywords(env: DiffEnv, frame: Frame, oldObj: JsonObject | undefined, newObj: JsonObject | undefined, operation: string): void {
  const emitted = new Set<string>();
  for (const [side, obj] of [['old', oldObj], ['new', newObj]] as const) {
    if (!obj) continue;
    for (const key of Object.keys(obj)) {
      if (key.startsWith('x-')) {
        const noteKey = `ext:${side}:${key}`;
        if (emitted.has(noteKey)) continue;
        emitted.add(noteKey);
        env.add({
          direction: env.direction,
          category: 'UNKNOWN_EXTENSION_PRESENT',
          severity: 'informational',
          operation,
          location: env.locAt(`${frame.location}.${key}`),
          message: `${side} schema carries extension ${key}; extensions are never judged breaking`,
          oldSide: side === 'old' ? `present (${key})` : 'absent',
          newSide: side === 'new' ? `present (${key})` : 'absent',
          witness: {
            description: 'extension metadata; not part of the wire value space',
            value: env.wrap(null),
            acceptedBy: 'extensions are ignored by this tool',
            rejectedBy: 'extensions are ignored by this tool',
          },
        });
        continue;
      }
      if (KNOWN_SCHEMA_KEYWORDS.has(key) || ANNOTATION_KEYWORDS.has(key)) continue;
      const noteKey = `kw:${key}`;
      if (emitted.has(noteKey)) continue;
      emitted.add(noteKey);
      env.add(undetermined(env, frame, operation, 'UNKNOWN_KEYWORD',
        `unsupported JSON Schema keyword "${key}" at ${side} side; effect on compatibility cannot be decided`,
        `"${key}" (e.g. pattern/minimum/oneOf/format) is outside the supported subset`));
    }
  }
}

function enumDelta(a: JsonValue[] | undefined, b: JsonValue[] | undefined): JsonValue[] {
  if (!a) return [];
  const bSet = new Set((b ?? []).map((v) => JSON.stringify(v)));
  return a.filter((v) => !bSet.has(JSON.stringify(v)));
}

function enumList(obj: JsonObject): JsonValue[] | undefined {
  return Array.isArray(obj['enum']) ? (obj['enum'] as JsonValue[]) : undefined;
}

interface BreakingArgs {
  env: DiffEnv;
  frame: Frame;
  operation: string;
  category: Finding['category'];
  locSuffix?: string;
  segments: string[];
  leaf: JsonValue;
  message: string;
  acceptedBy: string;
  rejectedBy: string;
}

function breaking(a: BreakingArgs): Omit<Finding, 'id'> {
  const sub = a.locSuffix
    ? (a.frame.location ? `${a.frame.location}.${a.locSuffix}` : a.locSuffix)
    : a.frame.location;
  return {
    direction: a.env.direction,
    category: a.category,
    severity: 'breaking',
    operation: a.operation,
    location: a.env.locAt(sub),
    message: a.message,
    oldSide: a.acceptedBy,
    newSide: a.rejectedBy,
    witness: {
      description: `${a.env.direction} witness for ${a.category}`,
      value: buildWitness(a.env, { segments: a.segments, leaf: a.leaf }),
      acceptedBy: a.acceptedBy,
      rejectedBy: a.rejectedBy,
    },
  };
}

function compareConstAndEnum(env: DiffEnv, frame: Frame, oldObj: JsonObject, newObj: JsonObject, operation: string): void {
  if (oldObj['const'] !== undefined && newObj['const'] !== undefined && !sameJson(oldObj['const'], newObj['const'])) {
    const leaf = env.direction === 'request' ? oldObj['const'] : newObj['const'];
    env.add(breaking({
      env, frame, operation, category: 'CONST_CHANGED', locSuffix: 'const',
      segments: baseSegments(frame), leaf: leaf as JsonValue,
      message: `const changed from ${JSON.stringify(oldObj['const'])} to ${JSON.stringify(newObj['const'])}`,
      acceptedBy: env.direction === 'request' ? `OLD contract requires const ${JSON.stringify(oldObj['const'])}` : `NEW contract produces const ${JSON.stringify(newObj['const'])}`,
      rejectedBy: env.direction === 'request' ? `NEW contract requires const ${JSON.stringify(newObj['const'])}` : `OLD client only accepts const ${JSON.stringify(oldObj['const'])}`,
    }));
  }

  const oldEnum = enumList(oldObj);
  const newEnum = enumList(newObj);
  if (oldEnum || newEnum) {
    const lost = enumDelta(oldEnum, newEnum);
    const gained = enumDelta(newEnum, oldEnum);
    const members = env.direction === 'request' ? lost : gained;
    if (members.length > 0) {
      const member = members[0] as JsonValue;
      env.add(breaking({
        env, frame, operation, category: 'ENUM_NARROWED', locSuffix: 'enum',
        segments: baseSegments(frame), leaf: member,
        message: env.direction === 'request'
          ? `enum removed member(s) ${JSON.stringify(members)} accepted by the OLD contract`
          : `enum gained member(s) ${JSON.stringify(members)} that OLD clients do not expect`,
        acceptedBy: env.direction === 'request' ? `OLD enum includes ${JSON.stringify(member)}` : `NEW response emits ${JSON.stringify(member)}`,
        rejectedBy: env.direction === 'request' ? `NEW enum is ${JSON.stringify(newEnum)}` : `OLD client enum is ${JSON.stringify(oldEnum)}`,
      }));
    }
  }
}

function compareTypes(env: DiffEnv, frame: Frame, oldObj: JsonObject, newObj: JsonObject, operation: string): void {
  const oldTypes = new Set(typesOf(oldObj));
  const newTypes = new Set(typesOf(newObj));
  const lost = [...oldTypes].filter((t) => !newTypes.has(t));
  const gained = [...newTypes].filter((t) => !oldTypes.has(t));

  const nullBrokeRequest = env.direction === 'request' && lost.includes('null');
  const nullBrokeResponse = env.direction === 'response' && gained.includes('null');
  if (nullBrokeRequest || nullBrokeResponse) {
    env.add(breaking({
      env, frame, operation, category: 'NULLABILITY_REMOVED', locSuffix: 'type',
      segments: baseSegments(frame), leaf: null,
      message: env.direction === 'request'
        ? 'value became non-nullable: null accepted by OLD is rejected by NEW'
        : 'value became nullable: NEW may emit null which an OLD client rejects',
      acceptedBy: env.direction === 'request' ? 'OLD type list includes "null"' : 'NEW type list includes "null"',
      rejectedBy: env.direction === 'request' ? `NEW types are ${JSON.stringify([...newTypes])}` : `OLD client types are ${JSON.stringify([...oldTypes])}`,
    }));
  }

  const nonNullBreaking = env.direction === 'request'
    ? lost.filter((t) => t !== 'null')
    : gained.filter((t) => t !== 'null');
  if (nonNullBreaking.length > 0) {
    const t = nonNullBreaking[0]!;
    env.add(breaking({
      env, frame, operation, category: 'TYPE_NARROWED', locSuffix: 'type',
      segments: baseSegments(frame), leaf: valueOfType(t),
      message: env.direction === 'request'
        ? `type "${t}" removed: values accepted by OLD are rejected by NEW`
        : `type "${t}" added to responses: OLD clients cannot accept it`,
      acceptedBy: env.direction === 'request' ? `OLD types include "${t}"` : `NEW response types include "${t}"`,
      rejectedBy: env.direction === 'request' ? `NEW types are ${JSON.stringify([...newTypes])}` : `OLD client types are ${JSON.stringify([...oldTypes])}`,
    }));
  }
}

function valueOfType(t: string): JsonValue {
  switch (t) {
    case 'string': return 'x';
    case 'integer':
    case 'number': return 0;
    case 'boolean': return true;
    case 'null': return null;
    case 'array': return [];
    case 'object': return {};
    default: return 'x';
  }
}

function compareDefault(env: DiffEnv, frame: Frame, oldObj: JsonObject, newObj: JsonObject, operation: string): void {
  const oldHas = Object.prototype.hasOwnProperty.call(oldObj, 'default');
  const newHas = Object.prototype.hasOwnProperty.call(newObj, 'default');
  if (oldHas && newHas && !sameJson(oldObj['default'], newObj['default'])) {
    env.add({
      direction: env.direction,
      category: 'DEFAULT_VALUE_CHANGED',
      severity: 'informational',
      operation,
      location: env.locAt(`${frame.location}.default`),
      message: `default changed from ${JSON.stringify(oldObj['default'])} to ${JSON.stringify(newObj['default'])}; omitted inputs resolve differently`,
      oldSide: `default ${JSON.stringify(oldObj['default'])}`,
      newSide: `default ${JSON.stringify(newObj['default'])}`,
      witness: {
        description: 'input omitting the field resolves to a different default',
        value: env.wrap(null),
        acceptedBy: `OLD fills default ${JSON.stringify(oldObj['default'])}`,
        rejectedBy: `NEW fills default ${JSON.stringify(newObj['default'])} (semantic change, not a rejection)`,
      },
    });
  }
}

function compareObjectShape(env: DiffEnv, frame: Frame, oldObj: JsonObject, newObj: JsonObject, operation: string): void {
  if (oldObj['items'] !== undefined || newObj['items'] !== undefined) {
    descend(env, frame, asSchema(oldObj['items']), asSchema(newObj['items']), 'items', operation);
  }

  const oldProps = asObj(oldObj['properties']);
  const newProps = asObj(newObj['properties']);
  if (!oldProps && !newProps) return;

  const oldKeys = new Set(oldProps ? Object.keys(oldProps) : []);
  const newKeys = new Set(newProps ? Object.keys(newProps) : []);
  const oldRequired = new Set(Array.isArray(oldObj['required']) ? (oldObj['required'] as JsonValue[]).map(String) : []);
  const newRequired = new Set(Array.isArray(newObj['required']) ? (newObj['required'] as JsonValue[]).map(String) : []);

  for (const key of new Set([...oldRequired, ...newRequired])) {
    const was = oldRequired.has(key);
    const is = newRequired.has(key);
    if (env.direction === 'request' && !was && is) {
      env.add(breaking({
        env, frame, operation, category: 'PROPERTY_MADE_REQUIRED', locSuffix: `required.${key}`,
        segments: [...baseSegments(frame), 'properties', key], leaf: OMIT,
        message: `property "${key}" became required; an OLD request without it is rejected by NEW`,
        acceptedBy: `OLD contract marks "${key}" optional`,
        rejectedBy: `NEW contract requires "${key}"`,
      }));
    }
    if (env.direction === 'response' && was && !is) {
      env.add(breaking({
        env, frame, operation, category: 'PROPERTY_REMOVED', locSuffix: `required.${key}`,
        segments: [...baseSegments(frame), 'properties', key], leaf: OMIT,
        message: `required response property "${key}" may now be omitted by NEW; an OLD client rejects its absence`,
        acceptedBy: `NEW contract allows omitting "${key}"`,
        rejectedBy: `OLD client requires "${key}"`,
      }));
    }
  }

  for (const key of new Set([...oldKeys, ...newKeys])) {
    const inOld = oldKeys.has(key);
    const inNew = newKeys.has(key);
    const oldChild = inOld && oldProps ? asSchema(oldProps[key]) : undefined;
    const newChild = inNew && newProps ? asSchema(newProps[key]) : undefined;
    if (inOld && inNew) {
      descend(env, frame, oldChild, newChild, `properties.${key}`, operation);
    } else if (inOld && !inNew) {
      removedProperty(env, frame, operation, key, oldChild);
    } else if (!inOld && inNew) {
      addedProperty(env, frame, operation, key, newChild);
    }
  }

  compareAdditionalProperties(env, frame, oldObj, newObj, operation);
}

function removedProperty(env: DiffEnv, frame: Frame, operation: string, key: string, oldChild: RawSchema | undefined): void {
  const newAllowsExtra = allowsExtra(frame.newNode, env.newResolver);
  if (env.direction === 'request' && !newAllowsExtra) {
    const v = sample(oldChild, newCtx(env.oldResolver)) ?? null;
    env.add(breaking({
      env, frame, operation, category: 'PROPERTY_REMOVED', locSuffix: `properties.${key}`,
      segments: [...baseSegments(frame), 'properties', key], leaf: v,
      message: `property "${key}" removed and additional properties forbidden; OLD requests carrying it are rejected`,
      acceptedBy: `OLD contract defines "${key}"`,
      rejectedBy: 'NEW contract: unknown property and additionalProperties=false',
    }));
  }
  if (env.direction === 'response') {
    env.add({
      direction: 'response',
      category: 'PROPERTY_REMOVED',
      severity: newAllowsExtra ? 'informational' : 'undetermined',
      operation,
      location: env.locAt(`${frame.location}.properties.${key}`),
      message: `response property "${key}" removed from NEW contract`,
      oldSide: `property "${key}" documented`,
      newSide: 'property absent',
      uncertainty: newAllowsExtra ? undefined : 'cannot tell whether the NEW server still emits the property',
      witness: {
        description: 'response property removal (clients typically ignore unknown/removed fields)',
        value: env.wrap(null),
        acceptedBy: 'OLD client schema documents the property',
        rejectedBy: newAllowsExtra ? 'not rejected: unknown properties ignored' : 'unknown',
      },
    });
  }
}

function addedProperty(env: DiffEnv, frame: Frame, operation: string, key: string, newChild: RawSchema | undefined): void {
  if (env.direction === 'response' && !allowsExtra(frame.oldNode, env.oldResolver)) {
    const v = sample(newChild, newCtx(env.newResolver)) ?? null;
    env.add(breaking({
      env, frame, operation, category: 'ADDITIONAL_PROPERTIES_TIGHTENED', locSuffix: `properties.${key}`,
      segments: [...baseSegments(frame), 'properties', key], leaf: v,
      message: `NEW response adds "${key}" but OLD clients forbid additional properties`,
      acceptedBy: 'NEW contract defines the property',
      rejectedBy: 'OLD client: additionalProperties=false',
    }));
  }
}

function allowsExtra(schema: RawSchema | undefined, resolver: SchemaResolver): boolean {
  const obj = asObj(deref(schema, newCtx(resolver)));
  if (!obj) return true;
  return obj['additionalProperties'] !== false;
}

function compareAdditionalProperties(env: DiffEnv, frame: Frame, oldObj: JsonObject, newObj: JsonObject, operation: string): void {
  const oldExtra = allowsExtra(frame.oldNode, env.oldResolver);
  const newExtra = allowsExtra(frame.newNode, env.newResolver);
  if (env.direction === 'request' && oldExtra && !newExtra) {
    env.add(breaking({
      env, frame, operation, category: 'ADDITIONAL_PROPERTIES_TIGHTENED', locSuffix: 'additionalProperties',
      segments: [...baseSegments(frame), 'properties', 'extra'], leaf: 1,
      message: 'additionalProperties changed to false; OLD requests with extra fields are rejected',
      acceptedBy: 'OLD contract allows additional properties',
      rejectedBy: 'NEW contract: additionalProperties=false',
    }));
  }
  if (env.direction === 'response' && !oldExtra && newExtra) {
    env.add(breaking({
      env, frame, operation, category: 'ADDITIONAL_PROPERTIES_TIGHTENED', locSuffix: 'additionalProperties',
      segments: [...baseSegments(frame), 'properties', 'extra'], leaf: 1,
      message: 'NEW responses may include extra properties; OLD clients with additionalProperties=false reject them',
      acceptedBy: 'NEW contract allows additional properties',
      rejectedBy: 'OLD client: additionalProperties=false',
    }));
  }
}

function descend(env: DiffEnv, frame: Frame, oldChild: RawSchema | undefined, newChild: RawSchema | undefined, sub: string, operation: string): void {
  const childLocation = frame.location ? `${frame.location}.${sub}` : sub;
  const pairKey = `${childLocation}::${JSON.stringify(oldChild ?? null)}::${JSON.stringify(newChild ?? null)}`;
  if (frame.seen.has(pairKey)) return; // cycle bound for recursive structures
  const nextSeen = new Set(frame.seen);
  nextSeen.add(pairKey);
  walk(env, { oldNode: oldChild, newNode: newChild, location: childLocation, depth: frame.depth + 1, seen: nextSeen }, operation);
}

// Re-export so callers can detect omission sentinels if needed.
export { isOmit };
