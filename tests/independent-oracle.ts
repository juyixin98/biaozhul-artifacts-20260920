/**
 * Independent acceptance oracle.
 *
 * The witness values produced by the diff kernel are checked here with Ajv
 * (a third-party JSON Schema validator) rather than by the kernel itself:
 *
 *   REQUEST breaking witness  -> MUST validate against the OLD schema,
 *                                MUST fail against the NEW schema.
 *   RESPONSE breaking witness -> MUST validate against the NEW schema,
 *                                MUST fail against the OLD schema.
 *
 * The parser/normalizer is reused to locate schemas, but the accept/reject
 * decision comes entirely from Ajv, so this is a genuine cross-check.
 */
import Ajv2020 from 'ajv/dist/2020.js';
import type { ErrorObject, ValidateFunction } from 'ajv';
import { randomUUID } from 'node:crypto';
import { parseContractDocument } from '../src/contract/loader.js';
import { normalizeDoc } from '../src/contract/normalize.js';
import type { JsonObject, JsonValue, OperationModel, ParamModel, RawSchema } from '../src/contract/types.js';
import type { DocumentBundle } from '../src/kernel/diff.js';

export interface ParsedSide {
  bundle: DocumentBundle;
  validateSchema: (schema: RawSchema | undefined, value: JsonValue) => boolean;
  /** Return Ajv errors (empty array means accepted). */
  schemaErrors: (schema: RawSchema | undefined, value: JsonValue) => ErrorObject[];
  op: (id: string) => OperationModel;
  raw: JsonObject;
}

function cloneWithRefs(node: JsonValue, baseId: string): JsonValue {
  if (Array.isArray(node)) return node.map((v) => cloneWithRefs(v, baseId));
  if (node !== null && typeof node === 'object') {
    const out: JsonObject = {};
    for (const [k, v] of Object.entries(node)) {
      if (k === '$ref' && typeof v === 'string' && v.startsWith('#/components/schemas/')) {
        const name = v.slice('#/components/schemas/'.length);
        out[k] = `${baseId}/${name}`;
      } else {
        out[k] = cloneWithRefs(v, baseId);
      }
    }
    return out;
  }
  return node;
}

function buildValidator(raw: JsonObject): (schema: RawSchema | undefined, value: JsonValue) => { valid: boolean; errors: ErrorObject[] } {
  const baseId = `https://local.test/${randomUUID()}`;
  const ajv = new Ajv2020.default({ strict: false, allErrors: true });
  const components = (raw['components'] as JsonObject | undefined)?.['schemas'];
  if (components && typeof components === 'object') {
    for (const [name, schema] of Object.entries(components as JsonObject)) {
      const rewritten = cloneWithRefs(schema as JsonValue, baseId);
      ajv.addSchema({ $id: `${baseId}/${name}`, ...(rewritten as JsonObject) });
    }
  }
  const cache = new Map<string, ValidateFunction>();
  return (schema, value) => {
    if (!schema || typeof schema !== 'object') return { valid: true, errors: [] };
    const key = JSON.stringify(schema);
    let validate = cache.get(key);
    if (!validate) {
      const rewritten = cloneWithRefs(schema, baseId) as JsonObject;
      validate = ajv.compile({ $id: `${baseId}/root-${cache.size}`, ...rewritten });
      cache.set(key, validate);
    }
    const valid = validate(value) as boolean;
    return { valid, errors: validate.errors ?? [] };
  };
}

export function parseSide(text: string): ParsedSide {
  const raw = parseContractDocument(text);
  const model = normalizeDoc(raw);
  const check = buildValidator(raw);

  return {
    bundle: { model, raw },
    raw,
    validateSchema: (schema, value) => check(schema, value).valid,
    schemaErrors: (schema, value) => check(schema, value).errors,
    op: (id) => {
      const found = model.operations.find((o) => o.id === id);
      if (!found) throw new Error(`operation ${id} not found`);
      return found;
    },
  };
}

/** Extract the body JSON from a kernel request/response witness value. */
export function witnessBody(witnessValue: JsonValue): JsonValue {
  const wrapper = witnessValue as JsonObject;
  if (typeof wrapper !== 'object' || wrapper === null) return wrapper;
  const req = wrapper['request'];
  if (req && typeof req === 'object') {
    const body = (req as JsonObject)['body'];
    return body ?? null;
  }
  const res = wrapper['response'];
  if (res && typeof res === 'object') {
    return (res as JsonObject)['body'] ?? null;
  }
  return wrapper;
}

export function witnessParam(witnessValue: JsonValue, carrier: string, name: string): JsonValue {
  const req = (witnessValue as JsonObject)['request'] as JsonObject | undefined;
  const slot = req?.[carrier];
  if (slot && typeof slot === 'object') return (slot as JsonObject)[name] ?? null;
  return null;
}

export type { ParamModel };
