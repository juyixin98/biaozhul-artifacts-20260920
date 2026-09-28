/**
 * Parser guarantees:
 * - bounded $ref resolution (cycles / depth cap / unresolved pointers)
 * - x-* extensions captured but never judged
 * - unsupported keywords surface as uncertainties instead of silent guessing
 */
import { describe, it, expect } from 'vitest';
import { ContractParser, REF_MAX_HOPS } from '../src/parser/contract-parser.js';
import { DiffEngine } from '../src/kernel/diff-engine.js';

function parse(document: unknown) {
  return new ContractParser().parse(document);
}

describe('contract parser: $ref handling', () => {
  it('resolves local component $refs', () => {
    const doc = {
      openapi: '3.1.0',
      info: { title: 't', version: '1' },
      paths: {
        '/pets': {
          get: {
            parameters: [{ $ref: '#/components/parameters/Tag' }],
            responses: { '200': { description: 'ok' } },
          },
        },
      },
      components: {
        parameters: {
          Tag: { name: 'tag', in: 'query', schema: { type: 'string' } },
        },
      },
    };
    const { contract, uncertainties } = parse(doc);
    expect(uncertainties).toEqual([]);
    const op = contract.operations.get('GET /pets')!;
    const tag = op.parameters.get('query:tag')!;
    expect(tag.schema.types).toEqual(['string']);
  });

  it('reports a $ref cycle and stays bounded', () => {
    const doc = {
      openapi: '3.1.0',
      info: { title: 't', version: '1' },
      paths: {
        '/nodes': {
          post: {
            requestBody: {
              required: false,
              content: { 'application/json': { schema: { $ref: '#/components/schemas/Node' } } },
            },
            responses: { '200': { description: 'ok' } },
          },
        },
      },
      components: {
        schemas: {
          Node: {
            type: 'object',
            properties: { child: { $ref: '#/components/schemas/Node' } },
          },
        },
      },
    };
    const { uncertainties } = parse(doc);
    expect(uncertainties.some((u) => u.code === 'REF_CYCLE')).toBe(true);
  });

  it('caps long $ref chains at REF_MAX_HOPS', () => {
    const schemas: Record<string, unknown> = {};
    for (let i = 0; i < REF_MAX_HOPS + 5; i += 1) {
      schemas[`S${i}`] = { $ref: `#/components/schemas/S${i + 1}` };
    }
    schemas[`S${REF_MAX_HOPS + 4}`] = { type: 'string' };
    const doc = {
      openapi: '3.1.0',
      info: { title: 't', version: '1' },
      paths: {
        '/x': {
          get: {
            parameters: [{ name: 'q', in: 'query', schema: { $ref: '#/components/schemas/S0' } }],
            responses: { '200': { description: 'ok' } },
          },
        },
      },
      components: { schemas },
    };
    const { uncertainties } = parse(doc);
    expect(uncertainties.some((u) => u.code === 'REF_DEPTH_LIMIT')).toBe(true);
  });

  it('flags unresolved pointers', () => {
    const doc = {
      openapi: '3.1.0',
      info: { title: 't', version: '1' },
      paths: {
        '/x': {
          get: {
            parameters: [{ name: 'q', in: 'query', schema: { $ref: '#/components/schemas/Missing' } }],
            responses: { '200': { description: 'ok' } },
          },
        },
      },
      components: { schemas: {} },
    };
    const { uncertainties } = parse(doc);
    expect(uncertainties.some((u) => u.code === 'UNRESOLVED_REF')).toBe(true);
  });
});

describe('contract parser: extensions and unsupported keywords', () => {
  it('keeps x-* extensions without judging them', () => {
    const doc = {
      openapi: '3.1.0',
      info: { title: 't', version: '1' },
      paths: {
        '/x': {
          'x-owner': 'team-payments',
          get: {
            'x-visibility': 'internal',
            parameters: [
              {
                name: 'q',
                in: 'query',
                'x-sensitive': true,
                schema: { type: 'string', 'x-widget': 'text' },
              },
            ],
            responses: { '200': { description: 'ok' } },
          },
        },
      },
    };
    const { contract, uncertainties } = parse(doc);
    expect(uncertainties).toEqual([]);
    const op = contract.operations.get('GET /x')!;
    const q = op.parameters.get('query:q')!;
    expect(q.schema.extensions['x-widget']).toBe('text');
  });

  it('does NOT let an unknown extension suppress a real nullability finding', () => {
    const old = {
      openapi: '3.1.0',
      info: { title: 't', version: '1' },
      'x-compat-policy': 'treat-null-as-ok',
      paths: {
        '/x': {
          get: {
            parameters: [{ name: 'q', in: 'query', schema: { type: ['string', 'null'], 'x-nullable': true } }],
            responses: { '200': { description: 'ok' } },
          },
        },
      },
    };
    const fresh = JSON.parse(JSON.stringify(old)) as typeof old;
    (fresh.paths['/x'].get.parameters![0]!.schema as { type: string[] }).type = ['string'];
    const { result } = new DiffEngine().diff(old, fresh, 'ext-not-judged');
    expect(result.findings.some((f) => f.code === 'PARAM_TYPE_NARROWED' && f.severity === 'BREAKING')).toBe(true);
  });

  it('flags oneOf/allOf as unsupported instead of guessing', () => {
    const doc = {
      openapi: '3.1.0',
      info: { title: 't', version: '1' },
      paths: {
        '/x': {
          post: {
            requestBody: {
              required: false,
              content: {
                'application/json': {
                  schema: { oneOf: [{ type: 'string' }, { type: 'number' }] },
                },
              },
            },
            responses: { '200': { description: 'ok' } },
          },
        },
      },
    };
    const { uncertainties } = parse(doc);
    expect(uncertainties.some((u) => u.code === 'UNSUPPORTED_KEYWORD')).toBe(true);
  });

  it('rejects a non-object document hard', () => {
    expect(() => parse([1, 2, 3])).toThrow();
  });

  it('records a 3.1 type array including null', () => {
    const { contract } = parse({
      openapi: '3.1.0',
      info: { title: 't', version: '1' },
      paths: {
        '/x': {
          get: {
            parameters: [{ name: 'q', in: 'query', schema: { type: ['string', 'null'] } }],
            responses: { '200': { description: 'ok' } },
          },
        },
      },
    });
    const q = contract.operations.get('GET /x')!.parameters.get('query:q')!;
    expect(q.schema.types).toEqual(['string', 'null']);
  });
});
