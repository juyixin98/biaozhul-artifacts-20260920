/**
 * Directionality tests: the SAME structural change must yield different
 * verdicts depending on whether values flow client->server (request) or
 * server->client (response). Also unit-tests the type lattice (integer is a
 * subtype of number, null is not silently accepted).
 */
import { describe, it, expect } from 'vitest';
import { DiffEngine } from '../src/kernel/diff-engine.js';
import { findRejectedType, isTypeAcceptable } from '../src/kernel/type-lattice.js';
import type { RawDoc } from './helpers/fixtures.js';

function makeDoc(version: string, bodySchema: unknown): RawDoc {
  return {
    openapi: '3.1.0',
    info: { title: 'asym', version },
    paths: {
      '/things': {
        post: {
          requestBody: { required: true, content: { 'application/json': { schema: bodySchema } } },
          responses: {
            '200': {
              description: 'ok',
              content: { 'application/json': { schema: bodySchema } },
            },
          },
        },
      },
    },
  } as unknown as RawDoc;
}

describe('type lattice', () => {
  it('treats integer as a subtype of number but not vice versa', () => {
    expect(isTypeAcceptable('integer', 'number')).toBe(true);
    expect(isTypeAcceptable('number', 'integer')).toBe(false);
  });

  it('rejects null unless null is explicitly accepted (3.1 semantics)', () => {
    expect(isTypeAcceptable('null', 'null')).toBe(true);
    expect(isTypeAcceptable('null', 'string')).toBe(false);
    expect(findRejectedType(['string', 'null'], ['string'])).toBe('null');
    expect(findRejectedType(['string'], ['string', 'null'])).toBeNull();
  });
});

describe('request vs response direction asymmetry', () => {
  it('optional -> required breaks requests but not responses', () => {
    const oldOptional = {
      type: 'object',
      properties: { region: { type: 'string' } },
      required: [],
    };
    const newRequired = {
      type: 'object',
      properties: { region: { type: 'string' } },
      required: ['region'],
    };
    const engine = new DiffEngine();
    const { result } = engine.diff(makeDoc('1', oldOptional), makeDoc('2', newRequired), 'dir-reqflip');

    const requestFlip = result.findings.find(
      (f) => f.direction === 'request' && f.code === 'REQUEST_BODY_FIELD_BECAME_REQUIRED',
    );
    const responseFlip = result.findings.find((f) => f.code === 'RESPONSE_FIELD_BECAME_REQUIRED');
    expect(requestFlip?.severity).toBe('BREAKING');
    expect(responseFlip?.severity).toBe('NON_BREAKING');
  });

  it('required -> optional is benign for requests but breaks responses', () => {
    const oldRequired = {
      type: 'object',
      properties: { region: { type: 'string' } },
      required: ['region'],
    };
    const newOptional = {
      type: 'object',
      properties: { region: { type: 'string' } },
      required: [],
    };
    const { result } = new DiffEngine().diff(makeDoc('1', oldRequired), makeDoc('2', newOptional), 'dir-optflip');

    const requestFlip = result.findings.find(
      (f) => f.direction === 'request' && f.code === 'REQUEST_BODY_FIELD_BECAME_OPTIONAL',
    );
    const responseFlip = result.findings.find((f) => f.code === 'RESPONSE_FIELD_BECAME_OPTIONAL');
    expect(requestFlip?.severity).toBe('NON_BREAKING');
    expect(responseFlip?.severity).toBe('BREAKING');
  });

  it('added field: request-side required is breaking, response-side added is benign', () => {
    const oldSchema = { type: 'object', properties: { a: { type: 'string' } }, required: ['a'] };
    const newSchema = {
      type: 'object',
      properties: { a: { type: 'string' }, b: { type: 'string' } },
      required: ['a', 'b'],
    };
    const { result } = new DiffEngine().diff(makeDoc('1', oldSchema), makeDoc('2', newSchema), 'dir-add');

    const requestAdded = result.findings.find((f) => f.code === 'REQUEST_BODY_REQUIRED_FIELD_ADDED');
    const responseAdded = result.findings.find((f) => f.code === 'RESPONSE_FIELD_ADDED');
    expect(requestAdded?.severity).toBe('BREAKING');
    expect(responseAdded?.severity).toBe('NON_BREAKING');
  });

  it('breaking findings carry distinct directions, not only request-side JSON deltas', () => {
    const { result } = new DiffEngine().diff(
      makeDoc('1', { type: 'string', enum: ['a', 'b'] }),
      makeDoc('2', { type: 'string', enum: ['a', 'b', 'c'] }),
      'dir-enum',
    );
    const requestEnum = result.findings.find((f) => f.direction === 'request');
    const responseEnum = result.findings.find((f) => f.direction === 'response');
    expect(requestEnum).toBeDefined();
    expect(responseEnum).toBeDefined();
    // request: new server accepts more -> non-breaking;
    // response: new server may emit 'c' -> breaking
    expect(requestEnum!.severity).toBe('NON_BREAKING');
    expect(responseEnum!.severity).toBe('BREAKING');
  });
});
