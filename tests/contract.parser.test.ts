import { describe, expect, it } from 'vitest';
import { parseContract, topoSort } from '../src/contract/parser.js';
import { buildScenario } from '../src/fixtures/scenario.js';

describe('contract parser', () => {
  it('rejects a non-object contract with INVALID_CONTRACT', () => {
    expect(() => parseContract(null as never)).toThrowError(/must be an object/);
  });

  it('rejects missing name', () => {
    expect(() =>
      parseContract({ version: 1, nodes: [], fields: [] } as never),
    ).toThrowError(/name must be/);
  });

  it('rejects a non-positive version', () => {
    expect(() =>
      parseContract({ name: 'x', version: 0, nodes: [], fields: [] }),
    ).toThrowError(/positive integer/);
  });

  it('rejects duplicate node ids', () => {
    const raw = {
      name: 'x',
      version: 1,
      nodes: [
        { id: 'a', source: 's' },
        { id: 'a', source: 's' },
      ],
      fields: [{ path: 'p', ref: { from: 'a' } }],
    };
    expect(() => parseContract(raw as never)).toThrowError(/duplicate node id: a/);
  });

  it('rejects a param reference to an unknown node', () => {
    const raw = {
      name: 'x',
      version: 1,
      nodes: [{ id: 'a', source: 's', params: { x: { from: 'ghost' } } }],
      fields: [],
    };
    expect(() => parseContract(raw as never)).toThrowError(/unknown node ghost/);
  });

  it('rejects a dependency cycle with the offending path', () => {
    const raw = {
      name: 'cyc',
      version: 2,
      nodes: [
        { id: 'a', source: 's', params: { x: { from: 'b' } } },
        { id: 'b', source: 's', params: { x: { from: 'a' } } },
      ],
      fields: [],
    };
    expect(() => parseContract(raw as never)).toThrowError(/dependency cycle/);
  });

  it('rejects self cycles', () => {
    const raw = {
      name: 'self',
      version: 1,
      nodes: [{ id: 'a', source: 's', params: { x: { from: 'a' } } }],
      fields: [],
    };
    expect(() => parseContract(raw as never)).toThrowError(/dependency cycle/);
  });

  it('rejects a field whose ref points at an undeclared node', () => {
    const raw = {
      name: 'x',
      version: 1,
      nodes: [{ id: 'a', source: 's' }],
      fields: [{ path: 'p', ref: { from: 'zzz' } }],
    };
    expect(() => parseContract(raw as never)).toThrowError(/must reference a declared node/);
  });

  it('rejects duplicate field paths', () => {
    const raw = {
      name: 'x',
      version: 1,
      nodes: [{ id: 'a', source: 's' }],
      fields: [
        { path: 'p', ref: { from: 'a' } },
        { path: 'p', ref: { constant: 1 } },
      ],
    };
    expect(() => parseContract(raw as never)).toThrowError(/duplicate field path: p/);
  });

  it('rejects an unknown necessity value', () => {
    const raw = {
      name: 'x',
      version: 1,
      nodes: [{ id: 'a', source: 's', necessity: 'maybe' }],
      fields: [],
    };
    expect(() => parseContract(raw as never)).toThrowError(/necessity/);
  });

  it('inherits optionality: a ref into an optional node is non-blocking', () => {
    const contract = parseContract({
      name: 'x',
      version: 1,
      nodes: [
        { id: 'a', source: 's', necessity: 'optional' },
        { id: 'b', source: 's', params: { x: { from: 'a', property: 'v' } } },
      ],
      fields: [],
    });
    const b = contract.nodes.find((n) => n.id === 'b')!;
    expect(b.params['x']).toMatchObject({ kind: 'ref', optional: true });
  });

  it('topological order puts every dependency before its dependents', () => {
    const contract = buildScenario().contract;
    const order = topoSort(contract);
    const pos = new Map(order.map((id, i) => [id, i]));
    for (const node of contract.nodes) {
      for (const param of Object.values(node.params)) {
        if (param.kind === 'ref') {
          expect(pos.get(param.fromNode)).toBeLessThan(pos.get(node.id)!);
        }
      }
    }
    // Deterministic start of the order: roots in declaration order.
    expect(order[0]).toBe('customers');
  });
});
