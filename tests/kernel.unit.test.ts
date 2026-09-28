import { describe, expect, it } from 'vitest';
import { parseContractDocument, ContractParseError, collectExtensions } from '../src/contract/loader.js';
import { normalizeDoc } from '../src/contract/normalize.js';
import { resolvePointer, resolveRef, MAX_REF_DEPTH } from '../src/contract/ref-resolver.js';
import { diffContracts, type DocumentBundle } from '../src/kernel/diff.js';
import type { FailureCategory } from '../src/kernel/types.js';
import { FIXTURES } from './fixtures/contracts.js';

function bundle(text: string): DocumentBundle {
  const raw = parseContractDocument(text);
  return { raw, model: normalizeDoc(raw) };
}

function categories(result: ReturnType<typeof diffContracts>, dir: 'requestFindings' | 'responseFindings'): Map<FailureCategory, number> {
  const m = new Map<FailureCategory, number>();
  for (const f of result[dir]) m.set(f.category, (m.get(f.category) ?? 0) + 1);
  return m;
}

describe('contract loader', () => {
  it('rejects non-3.1 documents with a position-tagged parse error', () => {
    const doc = JSON.stringify({ openapi: '3.0.3', paths: {} });
    expect(() => parseContractDocument(doc)).toThrow(ContractParseError);
    try {
      parseContractDocument(doc);
    } catch (e) {
      expect((e as ContractParseError).position).toBe('$.openapi');
    }
  });

  it('rejects malformed YAML with a parse error, not a raw stack', () => {
    expect(() => parseContractDocument('openapi: 3.1.0\npaths:\n  /x:\n   bad: {{\n')).toThrow(ContractParseError);
  });

  it('collects x-* extensions with their document paths', () => {
    const raw = parseContractDocument(FIXTURES.NEW);
    const exts = collectExtensions(raw);
    expect(exts.some((p) => p.endsWith('x-internal-hint'))).toBe(true);
  });
});

describe('ref resolver', () => {
  it('resolves local JSON pointers', () => {
    const raw = parseContractDocument(FIXTURES.OLD);
    const node = resolvePointer(raw, '#/components/schemas/Pet/required');
    expect(node).toEqual(['id', 'name', 'tags']);
  });

  it('flags missing refs instead of throwing', () => {
    const raw = parseContractDocument(FIXTURES.OLD);
    const res = resolveRef(raw, '#/components/schemas/Nope');
    expect(res.missing).toBe(true);
  });

  it('flags external refs as unsupported', () => {
    const raw = parseContractDocument(FIXTURES.OLD);
    const res = resolveRef(raw, 'https://example.com/external.yaml#/X');
    expect(res.external).toBe(true);
  });

  it('bounds cyclic refs with the on-path stack', () => {
    const cyclic = `
openapi: 3.1.0
info: { title: cyc, version: "1" }
paths: {}
components:
  schemas:
    Node:
      type: object
      properties:
        child:
          $ref: '#/components/schemas/Node'
`;
    const raw = parseContractDocument(cyclic);
    const stack: string[] = [];
    let res = resolveRef(raw, '#/components/schemas/Node', stack);
    let hops = 0;
    while (res.node && hops <= MAX_REF_DEPTH + 2) {
      hops += 1;
      stack.push(res.pointer);
      const next = resolveRef(raw, '#/components/schemas/Node', stack);
      if (next.cycle) break;
      res = next;
    }
    expect(hops).toBeGreaterThan(0);
    expect(resolveRef(raw, '#/components/schemas/Node', ['/components/schemas/Node']).cycle).toBe(true);
  });
});

describe('parameter normalization', () => {
  it('keys parameters by (name, location): same name at two locations stays distinct', () => {
    const model = bundle(FIXTURES.OLD).model;
    const list = model.operations.find((o) => o.id === 'GET /pets')!;
    const tenant = list.parameters.filter((p) => p.name === 'X-Tenant');
    expect(tenant).toHaveLength(1);
    expect(tenant[0]!.in).toBe('header');
  });
});

describe('diff matrix — REQUEST direction', () => {
  const result = diffContracts(bundle(FIXTURES.OLD), bundle(FIXTURES.NEW));
  const req = categories(result, 'requestFindings');

  it('flags the query->header? no: header->query move as PARAM_LOCATION_CHANGED', () => {
    const f = result.requestFindings.find((x) => x.category === 'PARAM_LOCATION_CHANGED');
    expect(f).toBeDefined();
    expect(f!.location).toContain('header -> query');
    expect(f!.severity).toBe('breaking');
    expect(JSON.stringify(f!.witness.value)).toContain('X-Tenant');
  });

  it('flags enum NARROWING on the request body (bird removed) but NOT enum extension on tag', () => {
    expect(req.get('ENUM_NARROWED')).toBe(1);
    const f = result.requestFindings.find((x) => x.category === 'ENUM_NARROWED')!;
    expect(f.operation).toBe('POST /pets');
    expect(JSON.stringify(f.witness.value)).toContain('bird');
  });

  it('treats the tag enum EXTENSION (cat,dog -> cat,dog,bird) as non-breaking', () => {
    const tagFinding = result.requestFindings.find(
      (x) => x.category === 'ENUM_NARROWED' && x.location.includes('tag'),
    );
    expect(tagFinding).toBeUndefined();
  });
  it('flags nullability removed on request body and on request parameter', () => {
    const nulls = result.requestFindings.filter((x) => x.category === 'NULLABILITY_REMOVED');
    expect(nulls).toHaveLength(2);
    expect(nulls.map((n) => n.operation).sort()).toEqual(['GET /pets', 'POST /pets']);
  });

  it('flags property made required (PetCreate.kind)', () => {
    const f = result.requestFindings.find((x) => x.category === 'PROPERTY_MADE_REQUIRED');
    expect(f).toBeDefined();
    expect(f!.location).toContain('kind');
  });

  it('flags existing and newly-introduced required parameters', () => {
    const f = result.requestFindings.filter((x) => x.category === 'PARAM_MADE_REQUIRED');
    const names = f.map((x) => x.location).join(' ');
    expect(names).toContain('trace');
    expect(names).toContain('limit');
  });

  it('flags requestBody becoming required', () => {
    expect(req.get('REQUEST_BODY_MADE_REQUIRED')).toBe(1);
  });

  it('flags removed operation', () => {
    expect(req.get('OPERATION_REMOVED')).toBe(1);
    const f = result.requestFindings.find((x) => x.category === 'OPERATION_REMOVED')!;
    expect(f.operation).toBe('GET /legacy');
  });

  it('records default-value change as informational, never breaking', () => {
    const f = result.requestFindings.find((x) => x.category === 'DEFAULT_VALUE_CHANGED' && x.location.includes('tag'));
    expect(f).toBeDefined();
    expect(f!.severity).toBe('informational');
  });

  it('lists every breaking finding with a concrete witness value', () => {
    for (const f of result.requestFindings.filter((x) => x.severity === 'breaking')) {
      expect(f.witness.value).toBeDefined();
      expect(f.witness.acceptedBy).not.toBe('');
      expect(f.witness.rejectedBy).not.toBe('');
    }
  });
});

describe('diff matrix — RESPONSE direction', () => {
  const result = diffContracts(bundle(FIXTURES.OLD), bundle(FIXTURES.NEW));
  const res = categories(result, 'responseFindings');

  it('flags newly produced status 500 (STATUS_CODE_SPLIT) but treats removed 404 as undetermined', () => {
    expect(res.get('STATUS_CODE_SPLIT')).toBe(1);
    const removed = result.responseFindings.find((x) => x.category === 'STATUS_CODE_REMOVED')!;
    expect(removed.severity).toBe('undetermined');
    expect(removed.uncertainty).toBeTruthy();
  });

  it('flags required response property becoming optional as a breaking omission witness', () => {
    const f = result.responseFindings.find((x) => x.category === 'PROPERTY_REMOVED' && x.severity === 'breaking');
    // tags: required old, optional new.
    expect(f).toBeDefined();
    expect(f!.location).toContain('tags');
  });

  it('flags added response property page under a strict (additionalProperties=false) old schema', () => {
    const f = result.responseFindings.find((x) => x.category === 'ADDITIONAL_PROPERTIES_TIGHTENED');
    expect(f).toBeDefined();
    expect(JSON.stringify(f!.witness.value)).toContain('page');
  });

  it('flags nullability ADDED to response id', () => {
    const f = result.responseFindings.find((x) => x.category === 'NULLABILITY_REMOVED');
    expect(f).toBeDefined();
    expect(f!.location).toContain('id');
  });

  it('does NOT flag nullability removed from server output as a response break', () => {
    const nameNull = result.responseFindings.find(
      (x) => x.category === 'NULLABILITY_REMOVED' && x.location.includes('name'),
    );
    expect(nameNull).toBeUndefined();
  });
});

describe('extensions and unknown keywords', () => {
  it('never judges x-* extensions breaking and reports them separately as notes', () => {
    const result = diffContracts(bundle(FIXTURES.OLD), bundle(FIXTURES.NEW));
    const breaking = [...result.requestFindings, ...result.responseFindings]
      .filter((f) => f.severity === 'breaking');
    expect(breaking.some((f) => f.category === 'UNKNOWN_EXTENSION_PRESENT')).toBe(false);
    expect(result.extensionNotes.some((n) => n.extension.endsWith('x-internal-hint') && n.change === 'added')).toBe(true);
  });

  it('downgrades unsupported keywords (pattern) to undetermined, not breaking', () => {
    const oldDoc = `openapi: 3.1.0
info: { title: t, version: "1" }
paths:
  /x:
    get:
      parameters:
        - { name: q, in: query, schema: { type: string, pattern: "^a" } }
      responses: { '200': { description: ok } }`;
    const newDoc = `openapi: 3.1.0
info: { title: t, version: "2" }
paths:
  /x:
    get:
      parameters:
        - { name: q, in: query, schema: { type: string, pattern: "^ab" } }
      responses: { '200': { description: ok } }`;
    const result = diffContracts(bundle(oldDoc), bundle(newDoc));
    const u = result.requestFindings.filter((f) => f.category === 'UNKNOWN_KEYWORD');
    expect(u.length).toBeGreaterThan(0);
    expect(u.every((f) => f.severity === 'undetermined')).toBe(true);
  });
});

describe('recursive $refs are bounded without false alarms on unchanged cycles', () => {
  it('produces no CYCLIC_REF finding when both Link trees are identical', () => {
    const result = diffContracts(bundle(FIXTURES.OLD), bundle(FIXTURES.NEW));
    const cyc = [...result.requestFindings, ...result.responseFindings]
      .filter((f) => f.category === 'CYCLIC_REF');
    expect(cyc).toHaveLength(0);
  });

  it('terminates (does not hang) on deeply different recursive schemas', () => {
    const make = (hrefType: string) => `openapi: 3.1.0
info: { title: cyc, version: "1" }
paths:
  /tree:
    get:
      parameters:
        - name: node
          in: query
          schema:
            $ref: '#/components/schemas/Node'
      responses: { '200': { description: ok } }
components:
  schemas:
    Node:
      type: object
      required: [href]
      properties:
        href:
          type: ${hrefType}
        next:
          $ref: '#/components/schemas/Node'`;
    const start = Date.now();
    const result = diffContracts(bundle(make('string')), bundle(make('integer')));
    expect(Date.now() - start).toBeLessThan(2000);
    expect(result.requestFindings.some((f) => f.category === 'TYPE_NARROWED')).toBe(true);
  });
});
