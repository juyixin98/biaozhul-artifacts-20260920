import { describe, expect, it } from 'vitest';
import { parseContractDocument } from '../src/contract/loader.js';
import { normalizeDoc } from '../src/contract/normalize.js';
import { resolvePointer, resolveRef, ensureLocalRefs } from '../src/contract/ref-resolver.js';
import { diffContracts, type DocumentBundle } from '../src/kernel/diff.js';
import { ContractStore } from '../src/state/store.js';
import { DiffService } from '../src/diagnostics/service.js';

function bundle(text: string): DocumentBundle {
  const raw = parseContractDocument(text);
  return { raw, model: normalizeDoc(raw) };
}

const base = (body: string) => `openapi: 3.1.0
info: { title: t, version: "1" }
paths:
  /x:
    ${body}
`;

describe('ref resolver edge cases', () => {
  it('walks arrays and returns undefined past the end', () => {
    const raw = parseContractDocument(FIXTURE());
    expect(resolvePointer(raw, '#/x/0')).toBe(1);
    expect(resolvePointer(raw, '#/x/5')).toBeUndefined();
    expect(resolvePointer(raw, '#/missing/a/b')).toBeUndefined();
    expect(resolvePointer(raw, '')).toBe(raw);
  });

  it('ensureLocalRefs reports unresolved and external refs', () => {
    const text = `openapi: 3.1.0
info: { title: t, version: "1" }
paths: {}
components:
  schemas:
    A:
      $ref: '#/components/schemas/Missing'
    B:
      $ref: 'https://elsewhere.example/s.yaml#/X'`;
    const raw = parseContractDocument(text);
    const bad = ensureLocalRefs(raw);
    expect(bad.some((b) => b.includes('unresolved'))).toBe(true);
    expect(bad.some((b) => b.includes('external refs unsupported'))).toBe(true);
  });

  it('resolveRef reports a cycle immediately when the pointer is on the stack', () => {
    const raw = parseContractDocument(FIXTURE());
    expect(resolveRef(raw, '#/Node', ['/Node']).cycle).toBe(true);
  });
});

function FIXTURE(): string {
  return `openapi: 3.1.0
info: { title: t, version: "1" }
x: [1, 2, 3]
paths:
  /n:
    get:
      parameters:
        - name: node
          in: query
          schema: { $ref: '#/Node' }
      responses: { '200': { description: ok } }`;
}

describe('request body edge cases', () => {
  it('flags an outright removed requestBody as breaking', () => {
    const oldDoc = base(`post:
      requestBody:
        required: false
        content:
          application/json:
            schema: { type: object }
      responses: { '201': { description: c } }`);
    const newDoc = base(`post:
      responses: { '201': { description: c } }`);
    const r = diffContracts(bundle(oldDoc), bundle(newDoc));
    expect(r.requestFindings.some((f) => f.category === 'REQUEST_BODY_REMOVED' && f.severity === 'breaking')).toBe(true);
  });

  it('flags a request content-type change as undetermined (cannot derive witness)', () => {
    const oldDoc = base(`post:
      requestBody:
        content:
          application/json:
            schema: { type: object }
      responses: { '201': { description: c } }`);
    const newDoc = base(`post:
      requestBody:
        content:
          application/xml:
            schema: { type: object }
      responses: { '201': { description: c } }`);
    const r = diffContracts(bundle(oldDoc), bundle(newDoc));
    const f = r.requestFindings.find((x) => x.category === 'RESPONSE_CONTENT_TYPE_REMOVED');
    expect(f?.severity).toBe('undetermined');
  });
});

describe('response content-type edge cases', () => {
  it('flags response content-type change json -> xml as breaking', () => {
    const oldDoc = base(`get:
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: { type: object }`);
    const newDoc = base(`get:
      responses:
        '200':
          description: ok
          content:
            application/xml:
              schema: { type: object }`);
    const r = diffContracts(bundle(oldDoc), bundle(newDoc));
    const f = r.responseFindings.find((x) => x.category === 'RESPONSE_CONTENT_TYPE_REMOVED');
    expect(f?.severity).toBe('breaking');
  });

  it('treats adding a default response as non-breaking', () => {
    const oldDoc = base(`get:
      responses:
        '200': { description: ok }`);
    const newDoc = base(`get:
      responses:
        '200': { description: ok }
        default: { description: fallback }`);
    const r = diffContracts(bundle(oldDoc), bundle(newDoc));
    expect(r.responseFindings.filter((f) => f.severity === 'breaking')).toHaveLength(0);
  });
});

describe('compatible contracts report no findings', () => {
  it('identical contracts are compatible', () => {
    const doc = base(`get:
      parameters:
        - name: q
          in: query
          schema: { type: string, enum: [a, b] }
      responses: { '200': { description: ok } }`);
    const r = diffContracts(bundle(doc), bundle(doc));
    expect(r.compatible).toBe(true);
    expect(r.requestFindings).toHaveLength(0);
    expect(r.responseFindings).toHaveLength(0);
  });

  it('adding an optional request parameter and a new operation is compatible', () => {
    const oldDoc = base(`get:
      responses: { '200': { description: ok } }`);
    const newDoc = `openapi: 3.1.0
info: { title: t, version: "2" }
paths:
  /x:
    get:
      parameters:
        - name: q
          in: query
          required: false
          schema: { type: string }
      responses: { '200': { description: ok } }
  /new:
    get:
      responses: { '200': { description: ok } }`;
    const r = diffContracts(bundle(oldDoc), bundle(newDoc));
    expect(r.compatible).toBe(true);
  });
});

describe('store analysis listing', () => {
  it('lists analyses, optionally filtered by request id', () => {
    const store = new ContractStore(':memory:');
    const svc = new DiffService(store);
    const doc = base(`get: { responses: { '200': { description: ok } } }`);
    svc.run({ oldContract: doc, newContract: doc }, 'req-a');
    svc.run({ oldContract: doc, newContract: doc }, 'req-b');
    expect(store.listAnalyses()).toHaveLength(2);
    expect(store.listAnalyses('req-a')).toHaveLength(1);
    expect(store.getAnalysis('does-not-exist')).toBeUndefined();
    store.close();
  });
});
