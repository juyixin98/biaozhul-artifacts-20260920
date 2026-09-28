/**
 * Directional schema comparison for the supported subset.
 *
 * The SAME primitive comparison is reused for both directions by swapping
 * which side produces values:
 *   - request direction: producer = OLD schema (values an old client can send)
 *   - response direction: producer = NEW schema (values a new server can emit)
 * A finding is BREAKING only when a producer-valid value is rejected by the
 * consumer. Each breaking finding carries a minimal concrete witness value.
 */
import type {
  Direction,
  FailureCode,
  JsonValue,
  NormalizedSchema,
  Severity,
  Witness,
} from '../core/types.js';
import {
  findRejectedType,
  isValueAcceptedBy,
  jsonEqual,
  sampleOfType,
} from './type-lattice.js';
import { sampleFromSchema } from './witness.js';

export type Segment =
  | { kind: 'property'; name: string }
  | { kind: 'item' };

export interface SchemaFinding {
  code: FailureCode;
  severity: Severity;
  /** JSON-pointer-ish path relative to the compared root schema */
  schemaPath: string;
  message: string;
  oldValue?: JsonValue | undefined;
  newValue?: JsonValue | undefined;
  witness: Witness | null;
}

interface CodeSet {
  typeNarrowed: FailureCode;
  enumNarrowed: FailureCode;
  enumRelaxed: FailureCode;
  defaultChanged: FailureCode;
  defaultRemoved: FailureCode;
  defaultAdded: FailureCode;
}

export interface DirectionHooks {
  direction: Direction;
  rootCodes: CodeSet;
  fieldCodes: CodeSet;
  classifyConsumerOnlyField: (
    name: string,
    consumerRequired: boolean,
  ) => { code: FailureCode; severity: Severity };
  classifyProducerOnlyField: (
    name: string,
    producerRequired: boolean,
  ) => { code: FailureCode; severity: Severity };
  /** Both sides define the property but their required flag differs. */
  classifyRequirementFlip: (
    name: string,
    consumerRequired: boolean,
    producerRequired: boolean,
  ) => { code: FailureCode; severity: Severity };
  /** Producer literals are a strict subset accepted by the consumer. */
  classifyEnumWidening: (
    addedLiterals: JsonValue[],
    atRoot: boolean,
  ) => { code: FailureCode; severity: Severity };
}

export interface CompareOptions {
  hooks: DirectionHooks;
  /** human-readable location prefix for witness rendering */
  baseLocation: string;
  /** wrap a field-level example into a full request/body witness */
  renderWitness: (
    schemaPath: string,
    segments: Segment[],
    leaf: JsonValue | undefined,
    rationale: string,
  ) => Witness;
}

export function compareSchemas(
  producer: NormalizedSchema,
  consumer: NormalizedSchema,
  opts: CompareOptions,
  segments: Segment[] = [],
  schemaPath = '',
): SchemaFinding[] {
  const findings: SchemaFinding[] = [];
  const codes = segments.length === 0 ? opts.hooks.rootCodes : opts.hooks.fieldCodes;
  const push = (f: SchemaFinding): void => {
    findings.push(f);
  };

  // 1) type surface ---------------------------------------------------------
  const rejectedType = findRejectedType(producer.types, consumer.types);
  if (rejectedType) {
    const leaf = typeEscapeValue(producer, rejectedType);
    push({
      code: codes.typeNarrowed,
      severity: 'BREAKING',
      schemaPath,
      message: `producer allows type "${rejectedType}" but consumer no longer accepts it`,
      oldValue: producer.types,
      newValue: consumer.types,
      witness: opts.renderWitness(
        schemaPath,
        segments,
        leaf,
        `value ${JSON.stringify(leaf)} is valid for the producer (types ${JSON.stringify(producer.types)}) but rejected by the consumer (types ${JSON.stringify(consumer.types)})`,
      ),
    });
    return findings; // structural comparison is meaningless when the type itself is gone
  }

  // 2) enum surface ---------------------------------------------------------
  if (producer.enum === null && consumer.enum !== null) {
    // producer is unrestricted, consumer pins an enum: find a concrete escape
    const enumEscape = findEnumEscape(producer, consumer);
    if (enumEscape !== null) {
      push({
        code: codes.enumNarrowed,
        severity: 'BREAKING',
        schemaPath,
        message: 'free-form producer value space was restricted to an enum the value escapes',
        oldValue: producer.enum,
        newValue: consumer.enum,
        witness: opts.renderWitness(
          schemaPath,
          segments,
          enumEscape,
          `value ${JSON.stringify(enumEscape)} is allowed by the producer but absent from the consumer enum ${JSON.stringify(consumer.enum)}`,
        ),
      });
    }
  } else if (producer.enum !== null && consumer.enum === null) {
    push({
      code: codes.enumRelaxed,
      severity: 'NON_BREAKING',
      schemaPath,
      message: 'consumer dropped the enum restriction; producer literals stay valid',
      oldValue: producer.enum,
      newValue: consumer.enum,
      witness: null,
    });
  } else if (producer.enum !== null && consumer.enum !== null) {
    const enumEscape = findEnumEscape(producer, consumer);
    if (enumEscape !== null) {
      push({
        code: codes.enumNarrowed,
        severity: 'BREAKING',
        schemaPath,
        message: 'consumer enum no longer covers every producer-allowed value',
        oldValue: producer.enum,
        newValue: consumer.enum,
        witness: opts.renderWitness(
          schemaPath,
          segments,
          enumEscape,
          `value ${JSON.stringify(enumEscape)} is allowed by the producer enum but absent from the consumer enum ${JSON.stringify(consumer.enum)}`,
        ),
      });
    } else {
      const added = consumer.enum.filter((v) => !producer.enum!.some((p) => jsonEqual(p, v)));
      if (added.length > 0) {
        const { code, severity } = opts.hooks.classifyEnumWidening(added, segments.length === 0);
        push({
          code,
          severity,
          schemaPath,
          message: opts.hooks.direction === 'request'
            ? `consumer accepts ${added.length} additional enum value(s): ${JSON.stringify(added)}`
            : `producer only emits a subset; consumer tolerates ${added.length} value(s) it no longer receives: ${JSON.stringify(added)}`,
          oldValue: producer.enum,
          newValue: consumer.enum,
          witness: null,
        });
      }
    }
  }

  // 3) default metadata (informational, never wire-breaking on its own) -----
  if (producer.hasDefault && consumer.hasDefault && !jsonEqual(producer.default, consumer.default)) {
    push({
      code: codes.defaultChanged,
      severity: 'NON_BREAKING',
      schemaPath,
      message: `default changed from ${JSON.stringify(producer.default)} to ${JSON.stringify(consumer.default)}`,
      oldValue: producer.default,
      newValue: consumer.default,
      witness: null,
    });
  } else if (producer.hasDefault && !consumer.hasDefault) {
    push({
      code: codes.defaultRemoved,
      severity: 'NON_BREAKING',
      schemaPath,
      message: `default ${JSON.stringify(producer.default)} was removed`,
      oldValue: producer.default,
      newValue: undefined,
      witness: null,
    });
  } else if (!producer.hasDefault && consumer.hasDefault) {
    push({
      code: codes.defaultAdded,
      severity: 'NON_BREAKING',
      schemaPath,
      message: `default ${JSON.stringify(consumer.default)} was added`,
      oldValue: undefined,
      newValue: consumer.default,
      witness: null,
    });
  }

  // 4) object structure -----------------------------------------------------
  if (allowsObject(producer) && allowsObject(consumer)) {
    // 4a) properties only the consumer declares (added burden / removed field)
    for (const [name, consumerProp] of consumer.properties) {
      if (producer.properties.has(name)) continue;
      const consumerRequired = consumer.required.has(name);
      const { code, severity } = opts.hooks.classifyConsumerOnlyField(
        name,
        consumerRequired,
      );
      const seg = [...segments, { kind: 'property' as const, name }];
      const path = joinPath(schemaPath, name);
      // minimal witness: a producer-valid object that simply lacks the field
      const witness =
        severity === 'BREAKING'
          ? opts.renderWitness(
              path,
              seg,
              undefined,
              consumerRequired
                ? `producer payload omits "${name}", but the consumer now marks it required`
                : `consumer declares "${name}" that the producer schema does not model`,
            )
          : null;
      push({
        code,
        severity,
        schemaPath: path,
        message: `property "${name}" exists only on the consumer side${
          consumerRequired ? ' (required)' : ''
        }`,
        oldValue: undefined,
        newValue: shapeOf(consumerProp),
        witness,
      });
    }

    // 4b) properties only the producer declares
    for (const [name, producerProp] of producer.properties) {
      if (consumer.properties.has(name)) continue;
      const producerRequired = producer.required.has(name);
      const { code, severity } = opts.hooks.classifyProducerOnlyField(
        name,
        producerRequired,
      );
      push({
        code,
        severity,
        schemaPath: joinPath(schemaPath, name),
        message: `property "${name}" exists only on the producer side${
          producerRequired ? ' (producer-required)' : ''
        }`,
        oldValue: shapeOf(producerProp),
        newValue: undefined,
        witness: null,
      });
    }

    // 4c) shared properties: required flips + recursive comparison
    for (const [name, producerProp] of producer.properties) {
      const consumerProp = consumer.properties.get(name);
      if (!consumerProp) continue;
      const path = joinPath(schemaPath, name);
      const seg = [...segments, { kind: 'property' as const, name }];
      const pReq = producer.required.has(name);
      const cReq = consumer.required.has(name);
      if (pReq !== cReq) {
        const { code, severity } = opts.hooks.classifyRequirementFlip(name, cReq, pReq);
        // Express the change in evolution (old -> new) terms, which inverts
        // for the response direction (producer=NEW, consumer=OLD).
        const oldRequired = opts.hooks.direction === 'request' ? pReq : cReq;
        const newRequired = opts.hooks.direction === 'request' ? cReq : pReq;
        push({
          code,
          severity,
          schemaPath: path,
          message: `property "${name}" changed ${oldRequired ? 'required' : 'optional'} -> ${
            newRequired ? 'required' : 'optional'
          }`,
          oldValue: pReq,
          newValue: cReq,
          witness:
            severity === 'BREAKING'
              ? opts.renderWitness(
                  path,
                  seg,
                  undefined,
                  cReq && !pReq
                    ? `producer may omit "${name}", but the consumer now requires it`
                    : `producer no longer guarantees "${name}", which the consumer requires`,
                )
              : null,
        });
      }
      findings.push(...compareSchemas(producerProp, consumerProp, opts, seg, path));
    }
  }

  // 5) array items ----------------------------------------------------------
  if (producer.items && consumer.items) {
    findings.push(
      ...compareSchemas(
        producer.items,
        consumer.items,
        opts,
        [...segments, { kind: 'item' as const }],
        schemaPath ? `${schemaPath}[]` : '$[]',
      ),
    );
  }

  return findings;
}

// ---------------------------------------------------------------------------

/** A producer-allowed value the consumer rejects, on the type/enum surface. */
function findEnumEscape(
  producer: NormalizedSchema,
  consumer: NormalizedSchema,
): JsonValue | null {
  // Consumer has no enum restriction -> nothing to escape.
  if (consumer.enum === null) return null;

  // Prefer a concrete producer enum literal.
  if (producer.enum !== null) {
    for (const literal of producer.enum) {
      if (!isValueAcceptedBy(literal, consumer)) return literal;
    }
    return null;
  }

  // Producer is unconstrained beyond its type: synthesize a representative
  // value that is producer-valid but outside the consumer enum.
  for (const t of producer.types.length > 0 ? producer.types : ['string'] as const) {
    const candidate = t === 'string' ? syntheticDistinctString(consumer.enum) : sampleOfType(t);
    if (!isValueAcceptedBy(candidate, consumer)) return candidate;
  }
  return null;
}

function syntheticDistinctString(forbidden: readonly JsonValue[]): JsonValue {
  const taken = new Set(
    forbidden.filter((v): v is string => typeof v === 'string'),
  );
  for (const candidate of ['other', 'unknown', 'x-other', 'zzz']) {
    if (!taken.has(candidate)) return candidate;
  }
  return `val-${forbidden.length}`;
}

function typeEscapeValue(
  producer: NormalizedSchema,
  rejectedType: NormalizedSchema['types'][number],
): JsonValue {
  // A producer enum literal of the rejected type makes the strongest witness.
  if (producer.enum) {
    for (const literal of producer.enum) {
      if (jsonTypeOf(literal) === rejectedType) return literal;
    }
  }
  return sampleOfType(rejectedType);
}

function jsonTypeOf(v: JsonValue): string {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}

function allowsObject(schema: NormalizedSchema): boolean {
  return schema.types.length === 0 || schema.types.includes('object');
}

function joinPath(base: string, name: string): string {
  return base ? `${base}.${name}` : `$.${name}`;
}

/** Compact shape descriptor for oldValue/newValue rendering. */
function shapeOf(schema: NormalizedSchema): JsonValue {
  return {
    types: schema.types,
    enum: schema.enum,
    required: [...schema.required],
    hasDefault: schema.hasDefault,
  };
}
