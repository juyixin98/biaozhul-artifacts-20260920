/**
 * Synthetic contract fixtures.
 *
 * Every matrix case is a hand-written {old, new} OpenAPI 3.1 pair — no
 * business data, no external services. The builder keeps fixtures terse while
 * the documents themselves stay real OpenAPI JSON.
 */
import type { JsonValue } from '../../src/core/types.js';

export type RawDoc = Record<string, JsonValue | undefined>;

interface PetOperationOptions {
  queryParams?: JsonValue[];
  headerParams?: JsonValue[];
  pathParams?: JsonValue[];
  requestBody?: JsonValue;
  responses?: Record<string, JsonValue>;
}

export function contract(
  version: string,
  paths: Record<string, Record<string, JsonValue>>,
  extras: RawDoc = {},
): RawDoc {
  return {
    openapi: '3.1.0',
    info: { title: 'pets-api', version },
    paths: paths as unknown as JsonValue,
    ...extras,
  };
}

export function param(name: string, inLoc: string, schema: JsonValue, extra: RawDoc = {}): JsonValue {
  return {
    name,
    in: inLoc,
    required: inLoc === 'path' ? undefined : false,
    schema,
    ...extra,
  } as JsonValue;
}

export function operation(opts: PetOperationOptions): JsonValue {
  const parameters = [
    ...(opts.pathParams ?? []),
    ...(opts.queryParams ?? []),
    ...(opts.headerParams ?? []),
  ];
  return {
    ...(parameters.length > 0 ? { parameters } : {}),
    ...(opts.requestBody !== undefined
      ? {
          requestBody: {
            required: true,
            content: { 'application/json': { schema: opts.requestBody } },
          },
        }
      : {}),
    responses:
      opts.responses ?? {
        '200': { description: 'ok' },
      },
  } as JsonValue;
}

export function jsonResponse(status: string, schema: JsonValue, description = 'ok'): JsonValue {
  return {
    [status]: {
      description,
      content: { 'application/json': { schema } },
    },
  } as unknown as JsonValue;
}

// ---------------------------------------------------------------------------
// The matrix: each fixture isolates one compatibility dimension.
// Expected outcome is asserted in test/matrix.test.ts, not encoded here.
// ---------------------------------------------------------------------------

export const matrix = {
  /** query param accepts null old-side only (3.1 type arrays). */
  nullableParamTightened(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': { get: operation({ queryParams: [param('tag', 'query', { type: ['string', 'null'] })] }) },
      }),
      new: contract('1.1.0', {
        '/pets': { get: operation({ queryParams: [param('tag', 'query', { type: 'string' })] }) },
      }),
    };
  },

  /** nullability widened: old clients sending strings stay valid. */
  nullableParamLoosened(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': { get: operation({ queryParams: [param('tag', 'query', { type: 'string' })] }) },
      }),
      new: contract('1.1.0', {
        '/pets': { get: operation({ queryParams: [param('tag', 'query', { type: ['string', 'null'] })] }) },
      }),
    };
  },

  /** request enum loses a literal. */
  enumNarrowed(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': {
          get: operation({
            queryParams: [param('status', 'query', { type: 'string', enum: ['available', 'pending', 'sold'] })],
          }),
        },
      }),
      new: contract('1.1.0', {
        '/pets': {
          get: operation({
            queryParams: [param('status', 'query', { type: 'string', enum: ['available', 'pending'] })],
          }),
        },
      }),
    };
  },

  /** request enum gains a literal (non-breaking). */
  enumExtended(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': {
          get: operation({
            queryParams: [param('status', 'query', { type: 'string', enum: ['available', 'pending'] })],
          }),
        },
      }),
      new: contract('1.1.0', {
        '/pets': {
          get: operation({
            queryParams: [
              param('status', 'query', { type: 'string', enum: ['available', 'pending', 'sold'] }),
            ],
          }),
        },
      }),
    };
  },

  /** request default value changes. */
  defaultChanged(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': { get: operation({ queryParams: [param('limit', 'query', { type: 'integer', default: 10 })] }) },
      }),
      new: contract('1.1.0', {
        '/pets': { get: operation({ queryParams: [param('limit', 'query', { type: 'integer', default: 25 })] }) },
      }),
    };
  },

  /** default removed then added on a request body field. */
  defaultRemovedOnBodyField(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': { post: operation({ requestBody: { type: 'object', properties: { limit: { type: 'integer', default: 10 } } } }) },
      }),
      new: contract('1.1.0', {
        '/pets': { post: operation({ requestBody: { type: 'object', properties: { limit: { type: 'integer' } } } }) },
      }),
    };
  },

  /** same parameter name moves query -> header. */
  paramMovedLocation(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': { get: operation({ queryParams: [param('trace', 'query', { type: 'string' })] }) },
      }),
      new: contract('1.1.0', {
        '/pets': { get: operation({ headerParams: [param('trace', 'header', { type: 'string' })] }) },
      }),
    };
  },

  /** path:id stays, a brand-new query:id is added (same name, other location). */
  sameNameSecondLocation(): { old: RawDoc; new: RawDoc } {
    const oldOp = operation({ pathParams: [param('id', 'path', { type: 'string' })] });
    const newOp = operation({
      pathParams: [param('id', 'path', { type: 'string' })],
      queryParams: [param('id', 'query', { type: 'string' })],
    });
    return {
      old: contract('1.0.0', { '/pets/{id}': { get: oldOp } }),
      new: contract('1.1.0', { '/pets/{id}': { get: newOp } }),
    };
  },

  /** optional parameter becomes required. */
  paramBecameRequired(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': { get: operation({ queryParams: [param('zone', 'query', { type: 'string' })] }) },
      }),
      new: contract('1.1.0', {
        '/pets': {
          get: operation({
            queryParams: [param('zone', 'query', { type: 'string' }, { required: true })],
          }),
        },
      }),
    };
  },

  /** brand-new required parameter. */
  requiredParamAdded(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', { '/pets': { get: operation({}) } }),
      new: contract('1.1.0', {
        '/pets': {
          get: operation({
            queryParams: [param('zone', 'query', { type: 'string' }, { required: true })],
          }),
        },
      }),
    };
  },

  /** request body gains a required field. */
  requiredBodyFieldAdded(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': {
          post: operation({
            requestBody: {
              type: 'object',
              properties: { name: { type: 'string' } },
              required: ['name'],
            },
          }),
        },
      }),
      new: contract('1.1.0', {
        '/pets': {
          post: operation({
            requestBody: {
              type: 'object',
              properties: { name: { type: 'string' }, owner: { type: 'string' } },
              required: ['name', 'owner'],
            },
          }),
        },
      }),
    };
  },

  /** nullable request field tightened. */
  nullableBodyFieldTightened(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': {
          post: operation({
            requestBody: {
              type: 'object',
              properties: { nickname: { type: ['string', 'null'] } },
            },
          }),
        },
      }),
      new: contract('1.1.0', {
        '/pets': {
          post: operation({
            requestBody: {
              type: 'object',
              properties: { nickname: { type: 'string' } },
            },
          }),
        },
      }),
    };
  },

  /** request body enum narrowed on a field. */
  bodyFieldEnumNarrowed(): { old: RawDoc; new: RawDoc } {
    const field = (enumValues: JsonValue): JsonValue => ({
      type: 'object',
      properties: { kind: { type: 'string', enum: enumValues } },
    });
    return {
      old: contract('1.0.0', { '/pets': { post: operation({ requestBody: field(['dog', 'cat', 'bird']) }) } }),
      new: contract('1.1.0', { '/pets': { post: operation({ requestBody: field(['dog', 'cat']) }) } }),
    };
  },

  /** new status code the old client has no branch for. */
  responseStatusAdded(): { old: RawDoc; new: RawDoc } {
    const added: PetOperationOptions['responses'] = {
      ...(jsonResponse('200', { type: 'object', properties: { ok: { type: 'boolean' } } }) as Record<string, JsonValue>),
      '429': { description: 'rate limited' },
    };
    return {
      old: contract('1.0.0', {
        '/pets': {
          get: operation({
            responses: jsonResponse('200', { type: 'object', properties: { ok: { type: 'boolean' } } }) as Record<string, JsonValue>,
          }),
        },
      }),
      new: contract('1.1.0', { '/pets': { get: operation({ responses: added }) } }),
    };
  },

  /** field the old response requires is gone in the new response. */
  responseRequiredFieldRemoved(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': {
          get: operation({
            responses: jsonResponse('200', {
              type: 'object',
              properties: { id: { type: 'string' }, name: { type: 'string' } },
              required: ['id', 'name'],
            }) as Record<string, JsonValue>,
          }),
        },
      }),
      new: contract('1.1.0', {
        '/pets': {
          get: operation({
            responses: jsonResponse('200', {
              type: 'object',
              properties: { id: { type: 'string' } },
              required: ['id'],
            }) as Record<string, JsonValue>,
          }),
        },
      }),
    };
  },

  /** response field changes required -> optional (server may now omit it). */
  responseFieldBecameOptional(): { old: RawDoc; new: RawDoc } {
    const schema = (required: JsonValue): JsonValue => ({
      type: 'object',
      properties: { id: { type: 'string' } },
      required,
    });
    return {
      old: contract('1.0.0', {
        '/pets': { get: operation({ responses: jsonResponse('200', schema(['id'])) as Record<string, JsonValue> }) },
      }),
      new: contract('1.1.0', {
        '/pets': { get: operation({ responses: jsonResponse('200', schema([])) as Record<string, JsonValue> }) },
      }),
    };
  },

  /** response enum gains a literal the old client cannot map. */
  responseEnumNarrowed(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', {
        '/pets': {
          get: operation({
            responses: jsonResponse('200', { type: 'string', enum: ['active', 'pending'] }) as Record<string, JsonValue>,
          }),
        },
      }),
      new: contract('1.1.0', {
        '/pets': {
          get: operation({
            responses: jsonResponse('200', {
              type: 'string',
              enum: ['active', 'pending', 'archived'],
            }) as Record<string, JsonValue>,
          }),
        },
      }),
    };
  },

  /** whole operation removed. */
  operationRemoved(): { old: RawDoc; new: RawDoc } {
    return {
      old: contract('1.0.0', { '/legacy': { delete: operation({}) } }),
      new: contract('1.1.0', { '/legacy': {} }),
    };
  },

  /** identical contracts: clean bill of health. */
  identical(): { old: RawDoc; new: RawDoc } {
    const doc = contract('1.0.0', { '/pets': { get: operation({}) } });
    return { old: doc, new: structuredClone(doc) };
  },
};
