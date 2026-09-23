import { describe, expect, it } from 'vitest';
import { compareLayouts } from '../src/index.js';
import { Finding, FindingCode, StorageEntry, StorageLayout, TypeEntry } from '../src/types.js';

const C = 'c.sol:C';

function entry(label: string, slot: string, type: string, offset = 0, contract = C): StorageEntry {
  return { astId: 1, contract, label, offset, slot, type };
}

const VALUE_TYPES: Record<string, TypeEntry> = {
  t_uint256: { encoding: 'inplace', label: 'uint256', numberOfBytes: '32' },
  t_uint128: { encoding: 'inplace', label: 'uint128', numberOfBytes: '16' },
  t_uint64: { encoding: 'inplace', label: 'uint64', numberOfBytes: '8' },
  t_address: { encoding: 'inplace', label: 'address', numberOfBytes: '20' },
  t_bool: { encoding: 'inplace', label: 'bool', numberOfBytes: '1' },
};

function layout(storage: StorageEntry[], types: Record<string, TypeEntry> = {}): StorageLayout {
  return { storage, types: { ...VALUE_TYPES, ...types } };
}

function codes(findings: Finding[]): FindingCode[] {
  return findings.map((f) => f.code);
}

function hasError(findings: Finding[], code: FindingCode, path?: string): boolean {
  return findings.some((f) => f.severity === 'error' && f.code === code && (path === undefined || f.path === path));
}

describe('top-level variables', () => {
  it('identical layouts are compatible with no findings', () => {
    const a = layout([entry('x', '0', 't_uint256'), entry('y', '1', 't_address')]);
    const r = compareLayouts(a, layout([entry('x', '0', 't_uint256'), entry('y', '1', 't_address')]));
    expect(r.verdict).toBe('compatible');
    expect(r.findings).toHaveLength(0);
  });

  it('appended fields are explicitly compatible', () => {
    const oldL = layout([entry('x', '0', 't_uint256')]);
    const newL = layout([entry('x', '0', 't_uint256'), entry('y', '1', 't_uint128'), entry('z', '1', 't_uint128', 16)]);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('compatible');
    expect(codes(r.findings)).toEqual(['FIELD_APPENDED', 'FIELD_APPENDED']);
    expect(r.findings[0]!.path).toBe('C.y');
    expect(r.findings[0]!.evidence.new?.slot).toBe('1');
  });

  it('a field inserted before existing storage is incompatible', () => {
    const oldL = layout([entry('x', '0', 't_uint256'), entry('y', '1', 't_uint256')]);
    const newL = layout([
      entry('inserted', '0', 't_uint256'),
      entry('x', '1', 't_uint256'),
      entry('y', '2', 't_uint256'),
    ]);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'FIELD_INSERTED', 'C.inserted')).toBe(true);
    expect(hasError(r.findings, 'FIELD_MOVED', 'C.x')).toBe(true);
    expect(hasError(r.findings, 'FIELD_MOVED', 'C.y')).toBe(true);
  });

  it('removed fields are incompatible', () => {
    const oldL = layout([entry('x', '0', 't_uint256'), entry('y', '1', 't_uint256')]);
    const newL = layout([entry('x', '0', 't_uint256')]);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'FIELD_REMOVED', 'C.y')).toBe(true);
  });

  it('packed-field offset changes are moves', () => {
    const oldL = layout([entry('a', '0', 't_uint128'), entry('b', '0', 't_uint128', 16)]);
    const newL = layout([entry('a', '0', 't_uint128'), entry('b', '1', 't_uint128', 0)]);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'FIELD_MOVED', 'C.b')).toBe(true);
    const f = r.findings.find((x) => x.code === 'FIELD_MOVED')!;
    expect(f.evidence.old?.offset).toBe(16);
    expect(f.evidence.new?.offset).toBe(0);
  });

  it('inheritance reorder moves are reported with the declaring contract change', () => {
    const oldL = layout([entry('x', '0', 't_uint256', 0, 'base.sol:Base'), entry('y', '1', 't_uint256')]);
    const newL = layout([entry('y', '0', 't_uint256'), entry('x', '1', 't_uint256', 0, 'other.sol:Other')]);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    const f = r.findings.find((x) => x.code === 'FIELD_MOVED' && x.path === 'C.x')!;
    expect(f.message).toContain('inheritance reorder');
  });
});

describe('types', () => {
  it('value type width change is incompatible', () => {
    const oldL = layout([entry('x', '0', 't_uint128')]);
    const newL = layout([entry('x', '0', 't_uint256')]);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'TYPE_WIDTH_CHANGED', 'C.x')).toBe(true);
    const f = r.findings[0]!;
    expect(f.evidence.old?.numberOfBytes).toBe('16');
    expect(f.evidence.new?.numberOfBytes).toBe('32');
  });

  it('encoding kind change is incompatible', () => {
    const types = {
      't_mapping(t_address,t_uint256)': {
        encoding: 'mapping',
        label: 'mapping(address => uint256)',
        numberOfBytes: '32',
        key: 't_address',
        value: 't_uint256',
      } as TypeEntry,
    };
    const oldL = layout([entry('x', '0', 't_uint256')]);
    const newL = layout([entry('x', '0', 't_mapping(t_address,t_uint256)')], types);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'TYPE_KIND_CHANGED', 'C.x')).toBe(true);
  });

  it('mapping value type change is reported at the nested path', () => {
    const oldTypes = {
      't_mapping(t_address,t_uint256)': {
        encoding: 'mapping', label: 'mapping(address => uint256)', numberOfBytes: '32',
        key: 't_address', value: 't_uint256',
      } as TypeEntry,
    };
    const newTypes = {
      't_mapping(t_address,t_uint128)': {
        encoding: 'mapping', label: 'mapping(address => uint128)', numberOfBytes: '32',
        key: 't_address', value: 't_uint128',
      } as TypeEntry,
    };
    const oldL = layout([entry('balances', '0', 't_mapping(t_address,t_uint256)')], oldTypes);
    const newL = layout([entry('balances', '0', 't_mapping(t_address,t_uint128)')], newTypes);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'MAPPING_TYPE_CHANGED', 'C.balances')).toBe(true);
    expect(hasError(r.findings, 'TYPE_WIDTH_CHANGED', 'C.balances.<value>')).toBe(true);
  });

  it('dynamic array element change is incompatible', () => {
    const oldTypes = {
      't_array(t_uint256)dyn_storage': {
        encoding: 'dynamic_array', label: 'uint256[]', numberOfBytes: '32', base: 't_uint256',
      } as TypeEntry,
    };
    const newTypes = {
      't_array(t_address)dyn_storage': {
        encoding: 'dynamic_array', label: 'address[]', numberOfBytes: '32', base: 't_address',
      } as TypeEntry,
    };
    const oldL = layout([entry('xs', '0', 't_array(t_uint256)dyn_storage')], oldTypes);
    const newL = layout([entry('xs', '0', 't_array(t_address)dyn_storage')], newTypes);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'ARRAY_BASE_CHANGED', 'C.xs')).toBe(true);
  });

  it('static array length change is incompatible', () => {
    const oldTypes = {
      't_array(t_uint256)50_storage': {
        encoding: 'inplace', label: 'uint256[50]', numberOfBytes: '1600', base: 't_uint256',
      } as TypeEntry,
    };
    const newTypes = {
      't_array(t_uint256)49_storage': {
        encoding: 'inplace', label: 'uint256[49]', numberOfBytes: '1568', base: 't_uint256',
      } as TypeEntry,
    };
    const oldL = layout([entry('arr', '0', 't_array(t_uint256)50_storage')], oldTypes);
    const newL = layout([entry('arr', '0', 't_array(t_uint256)49_storage')], newTypes);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'ARRAY_LENGTH_CHANGED', 'C.arr')).toBe(true);
  });
});

describe('structs', () => {
  const oldStruct = {
    't_struct(S)1_storage': {
      encoding: 'inplace', label: 'struct S', numberOfBytes: '32',
      members: [
        { astId: 1, contract: C, label: 'a', offset: 0, slot: '0', type: 't_uint128' },
        { astId: 2, contract: C, label: 'b', offset: 16, slot: '0', type: 't_uint128' },
      ],
    } as TypeEntry,
  };

  it('appended struct member is compatible', () => {
    const newStruct = {
      't_struct(S)2_storage': {
        encoding: 'inplace', label: 'struct S', numberOfBytes: '64',
        members: [
          { astId: 1, contract: C, label: 'a', offset: 0, slot: '0', type: 't_uint128' },
          { astId: 2, contract: C, label: 'b', offset: 16, slot: '0', type: 't_uint128' },
          { astId: 3, contract: C, label: 'c', offset: 0, slot: '1', type: 't_uint64' },
        ],
      } as TypeEntry,
    };
    const oldL = layout([entry('s', '0', 't_struct(S)1_storage')], oldStruct);
    const newL = layout([entry('s', '0', 't_struct(S)2_storage')], newStruct);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('compatible');
    expect(codes(r.findings)).toEqual(['STRUCT_MEMBER_APPENDED']);
    expect(r.findings[0]!.path).toBe('C.s.c');
  });

  it('inserted struct member is incompatible and shifts siblings', () => {
    const newStruct = {
      't_struct(S)2_storage': {
        encoding: 'inplace', label: 'struct S', numberOfBytes: '32',
        members: [
          { astId: 1, contract: C, label: 'a', offset: 0, slot: '0', type: 't_uint64' },
          { astId: 9, contract: C, label: 'mid', offset: 8, slot: '0', type: 't_uint64' },
          { astId: 2, contract: C, label: 'b', offset: 16, slot: '0', type: 't_uint128' },
        ],
      } as TypeEntry,
    };
    const oldL = layout([entry('s', '0', 't_struct(S)1_storage')], oldStruct);
    const newL = layout([entry('s', '0', 't_struct(S)2_storage')], newStruct);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'STRUCT_MEMBER_INSERTED', 'C.s.mid')).toBe(true);
  });

  it('removed struct member is incompatible', () => {
    const newStruct = {
      't_struct(S)2_storage': {
        encoding: 'inplace', label: 'struct S', numberOfBytes: '16',
        members: [{ astId: 1, contract: C, label: 'a', offset: 0, slot: '0', type: 't_uint128' }],
      } as TypeEntry,
    };
    const oldL = layout([entry('s', '0', 't_struct(S)1_storage')], oldStruct);
    const newL = layout([entry('s', '0', 't_struct(S)2_storage')], newStruct);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'STRUCT_MEMBER_REMOVED', 'C.s.b')).toBe(true);
  });

  it('nested struct member width change surfaces the deepest path', () => {
    const oldTypes = {
      't_struct(Inner)1_storage': {
        encoding: 'inplace', label: 'struct Inner', numberOfBytes: '32',
        members: [{ astId: 1, contract: C, label: 'n', offset: 0, slot: '0', type: 't_uint256' }],
      } as TypeEntry,
      't_struct(Outer)1_storage': {
        encoding: 'inplace', label: 'struct Outer', numberOfBytes: '32',
        members: [{ astId: 2, contract: C, label: 'inner', offset: 0, slot: '0', type: 't_struct(Inner)1_storage' }],
      } as TypeEntry,
    };
    const newTypes = {
      't_struct(Inner)2_storage': {
        encoding: 'inplace', label: 'struct Inner', numberOfBytes: '32',
        members: [{ astId: 1, contract: C, label: 'n', offset: 0, slot: '0', type: 't_uint128' }],
      } as TypeEntry,
      't_struct(Outer)2_storage': {
        encoding: 'inplace', label: 'struct Outer', numberOfBytes: '32',
        members: [{ astId: 2, contract: C, label: 'inner', offset: 0, slot: '0', type: 't_struct(Inner)2_storage' }],
      } as TypeEntry,
    };
    const oldL = layout([entry('o', '0', 't_struct(Outer)1_storage')], oldTypes);
    const newL = layout([entry('o', '0', 't_struct(Outer)2_storage')], newTypes);
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'TYPE_WIDTH_CHANGED', 'C.o.inner.n')).toBe(true);
  });
});

describe('storage gaps', () => {
  const gapTypes = (n: number) => ({
    [`t_array(t_uint256)${n}_storage`]: {
      encoding: 'inplace', label: `uint256[${n}]`, numberOfBytes: String(n * 32), base: 't_uint256',
    } as TypeEntry,
  });

  it('consistent gap reduction is compatible', () => {
    const oldL = layout(
      [entry('_owner', '0', 't_address'), entry('__gap', '1', 't_array(t_uint256)50_storage')],
      gapTypes(50),
    );
    const newL = layout(
      [
        entry('_owner', '0', 't_address'),
        entry('_treasury', '1', 't_address'),
        entry('_feeBps', '1', 't_uint64', 20),
        entry('__gap', '2', 't_array(t_uint256)49_storage'),
      ],
      gapTypes(49),
    );
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('compatible');
    expect(codes(r.findings)).toContain('GAP_REDUCED');
    expect(codes(r.findings)).toContain('FIELD_APPENDED');
  });

  it('gap shrink without matching consumption is a mismatch', () => {
    const oldL = layout(
      [entry('_owner', '0', 't_address'), entry('__gap', '1', 't_array(t_uint256)50_storage')],
      gapTypes(50),
    );
    const newL = layout(
      [entry('_owner', '0', 't_address'), entry('__gap', '1', 't_array(t_uint256)40_storage')],
      gapTypes(40),
    );
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'GAP_MISMATCH', 'C.__gap')).toBe(true);
  });

  it('gap boundary: a new field one slot past the gap end is a plain append', () => {
    const oldL = layout(
      [entry('_owner', '0', 't_address'), entry('__gap', '1', 't_array(t_uint256)2_storage')],
      gapTypes(2),
    );
    const newL = layout(
      [
        entry('_owner', '0', 't_address'),
        entry('_a', '1', 't_uint256'),
        entry('_b', '2', 't_uint256'),
        entry('_c', '3', 't_uint256'),
      ],
      {},
    );
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('compatible');
    expect(codes(r.findings)).toContain('GAP_REDUCED');
  });

  it('gap boundary: consuming gap slots without shrinking the gap is a mismatch', () => {
    const oldL = layout(
      [entry('_owner', '0', 't_address'), entry('__gap', '1', 't_array(t_uint256)2_storage'), entry('_tail', '3', 't_uint256')],
      gapTypes(2),
    );
    // _rogue sits at slot 2 (inside old gap span) but _tail follows the gap:
    // the gap is not the last variable, so slot 2 is not free-append space.
    const newL = layout(
      [
        entry('_owner', '0', 't_address'),
        entry('_rogue', '2', 't_uint256'),
        entry('__gap', '1', 't_array(t_uint256)2_storage'),
        entry('_tail', '3', 't_uint256'),
      ],
      gapTypes(2),
    );
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('incompatible');
    expect(hasError(r.findings, 'GAP_MISMATCH')).toBe(true);
  });
});

describe('unknown types are conservative', () => {
  it('missing type descriptor yields verdict "unknown", never "compatible"', () => {
    const oldL: StorageLayout = { storage: [entry('mystery', '0', 't_custom')], types: {} };
    const newL: StorageLayout = { storage: [entry('mystery', '0', 't_custom')], types: {} };
    const r = compareLayouts(oldL, newL);
    expect(r.verdict).toBe('unknown');
    expect(codes(r.findings)).toContain('UNKNOWN_TYPE');
  });

  it('unrecognized encoding yields verdict "unknown"', () => {
    const weird = {
      t_weird: { encoding: 'quantum', label: 'weird', numberOfBytes: '32' } as TypeEntry,
    };
    const oldL = layout([entry('x', '0', 't_weird')], weird);
    const newL = layout([entry('x', '0', 't_weird')], weird);
    const r = compareLayouts(oldL, newL);
    // identical type id short-circuits only when described; here it IS described
    // in both, so identical ids are compatible without decoding.
    expect(r.verdict).toBe('compatible');

    const oldL2 = layout([entry('x', '0', 't_weird')], weird);
    const newL2 = layout([entry('x', '0', 't_weird2')], {
      t_weird2: { encoding: 'quantum', label: 'weird', numberOfBytes: '32' } as TypeEntry,
    });
    const r2 = compareLayouts(oldL2, newL2);
    expect(r2.verdict).toBe('unknown');
    expect(codes(r2.findings)).toContain('UNKNOWN_ENCODING');
  });
});
