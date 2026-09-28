/**
 * Independent witness tests.
 *
 * These tests do NOT trust the kernel's accept/reject labels: each breaking
 * witness is fed to Ajv (independent third-party validator) against both the
 * OLD and NEW contract schemas. A request-direction breaking witness MUST
 * validate OLD and fail NEW; a response-direction one MUST validate NEW and
 * fail OLD. Protocol-level findings (removed operation, required parameter,
 * status codes) have no JSON-Schema witness and are asserted structurally.
 */
import { describe, expect, it } from 'vitest';
import { diffContracts } from '../src/kernel/diff.js';
import type { Finding, JsonValue } from '../src/kernel/types.js';
import { FIXTURES } from './fixtures/contracts.js';
import { parseSide, witnessBody, witnessParam, type ParsedSide } from './independent-oracle.js';
import type { OperationModel, ParamModel, RawSchema } from '../src/contract/types.js';

const oldSide = parseSide(FIXTURES.OLD);
const newSide = parseSide(FIXTURES.NEW);
const result = diffContracts(oldSide.bundle, newSide.bundle);

const PROTOCOL_CATEGORIES = new Set<Finding['category']>([
  'OPERATION_REMOVED',
  'PARAM_LOCATION_CHANGED',
  'PARAM_MADE_REQUIRED',
  'PARAM_REMOVED',
  'REQUEST_BODY_MADE_REQUIRED',
  'REQUEST_BODY_REMOVED',
  'STATUS_CODE_REMOVED',
  'STATUS_CODE_SPLIT',
  'RESPONSE_CONTENT_TYPE_REMOVED',
  'DEFAULT_VALUE_CHANGED',
  'UNKNOWN_EXTENSION_PRESENT',
]);

function responseSchemaFor(side: ParsedSide, operation: string, status: string): RawSchema | undefined {
  const op: OperationModel = side.op(operation);
  return op.responses.find((r) => r.status === status)?.schema;
}

function requestBodySchemaFor(side: ParsedSide, operation: string): RawSchema | undefined {
  return side.op(operation).requestBody?.schema;
}

function findParam(side: ParsedSide, operation: string, location: string): ParamModel | undefined {
  // locations look like parameters[query:tag].schema... or [header:trace]
  const m = /parameters\[(query|header|path|cookie):([^\]]+)\]/.exec(location);
  if (!m) return undefined;
  const [, loc, name] = m as unknown as [string, ParamModel['in'], string];
  return side.op(operation).parameters.find((p) => p.in === loc && p.name === name);
}

function assertValid(actual: boolean, expected: boolean, detail: unknown): void {
  if (actual !== expected) {
    throw new Error(`expected schema validation ${expected ? 'to PASS' : 'to FAIL'} but it ${actual ? 'passed' : 'failed'}; ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
  }
}

describe('independent Ajv oracle — every breaking request witness is accepted OLD / rejected NEW', () => {
  const breaking = result.requestFindings.filter((f) => f.severity === 'breaking');

  it('has a non-trivial set of breaking request findings', () => {
    expect(breaking.length).toBeGreaterThanOrEqual(7);
  });

  for (const f of breaking) {
    it(`[${f.category}] ${f.operation} @ ${f.location}`, () => {
      if (PROTOCOL_CATEGORIES.has(f.category)) {
        // Structural assertion for protocol-level findings.
        expect(f.witness.value).not.toBeNull();
        return;
      }
      const bodySchemaOld = requestBodySchemaFor(oldSide, f.operation);
      const bodySchemaNew = requestBodySchemaFor(newSide, f.operation);
      const param = findParam(oldSide, f.operation, f.location);

      if (param) {
        const carrier = param.in;
        const v = witnessParam(f.witness.value, carrier, param.name);
        const newParam = findParam(newSide, f.operation, f.location);
        assertValid(oldSide.validateSchema(param.schema, v), true,
          { requirement: 'validate OLD param schema', errors: oldSide.schemaErrors(param.schema, v) });
        if (newParam) {
          assertValid(newSide.validateSchema(newParam.schema, v), false,
            { requirement: 'rejected by NEW param schema' });
        }
        return;
      }

      const body = witnessBody(f.witness.value);
      assertValid(oldSide.validateSchema(bodySchemaOld, body), true,
        { requirement: 'validate OLD body schema', errors: oldSide.schemaErrors(bodySchemaOld, body) });
      assertValid(newSide.validateSchema(bodySchemaNew, body), false,
        { requirement: 'rejected by NEW body schema' });
    });
  }
});

describe('independent Ajv oracle — every breaking response witness is accepted NEW / rejected OLD', () => {
  const breaking = result.responseFindings.filter((f) => f.severity === 'breaking');

  it('has a non-trivial set of breaking response findings', () => {
    expect(breaking.length).toBeGreaterThanOrEqual(3);
  });

  for (const f of breaking) {
    it(`[${f.category}] ${f.operation} @ ${f.location}`, () => {
      if (PROTOCOL_CATEGORIES.has(f.category)) {
        expect(f.witness.value).not.toBeNull();
        return;
      }
      const status = /responses\.(\d+)/.exec(f.location)?.[1] ?? '200';
      const schemaNew = responseSchemaFor(newSide, f.operation, status);
      const schemaOld = responseSchemaFor(oldSide, f.operation, status);
      const body = witnessBody(f.witness.value);
      assertValid(newSide.validateSchema(schemaNew, body), true,
        { requirement: 'validate NEW response schema', errors: newSide.schemaErrors(schemaNew, body) });
      assertValid(oldSide.validateSchema(schemaOld, body), false,
        { requirement: 'rejected by OLD client schema' });
    });
  }
});

describe('undetermined findings never claim a concrete rejection', () => {
  const uncertain = [
    ...result.requestFindings,
    ...result.responseFindings,
  ].filter((f) => f.severity === 'undetermined');

  it('includes the removed 404 status as undetermined with a reason', () => {
    const removed = uncertain.find((f) => f.category === 'STATUS_CODE_REMOVED');
    expect(removed).toBeDefined();
    expect(removed!.uncertainty).toMatch(/does not prove|cannot tell/i);
  });

  for (const f of uncertain) {
    it(`[${f.category}] ${f.operation} carries an explicit uncertainty reason`, () => {
      expect(typeof f.uncertainty).toBe('string');
      expect(f.uncertainty!.length).toBeGreaterThan(5);
    });
  }
});

describe('minimality: witnesses contain no unrelated payload beyond what is required', () => {
  it('enum witness for kind=bird contains only required name plus the offending leaf', () => {
    const f = result.requestFindings.find((x) => x.category === 'ENUM_NARROWED')!;
    const body = witnessBody(f.witness.value) as Record<string, JsonValue>;
    expect(Object.keys(body).sort()).toEqual(['kind', 'name']);
    expect(body['kind']).toBe('bird');
  });

  it('required-property witness omits exactly the offending property', () => {
    const f = result.requestFindings.find((x) => x.category === 'PROPERTY_MADE_REQUIRED')!;
    const body = witnessBody(f.witness.value) as Record<string, JsonValue>;
    expect(Object.keys(body)).toEqual(['name']);
  });
});
