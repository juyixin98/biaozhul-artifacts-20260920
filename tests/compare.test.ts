/**
 * 检查器单元/集成测试（node:test）。
 * 覆盖：打包追加、打包插入、gap 缩小/清空/越界/移动、嵌套结构体末端与非末端、
 * mapping/变长数组内追加、定长数组、mapping 键、位宽、enum、继承、未知类型、
 * 最短路径与证据、HTTP 服务端到端。
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { compareLayouts } from '../src/compare.js';
import { check } from '../src/index.js';
import type { Finding, FindingKind, SolcStorageLayout } from '../src/types.js';

/* --------------------------- 手工布局构造助手 --------------------------- */

interface FieldOpts {
  name: string;
  type?: string;
  slot?: number | string;
  offset?: number;
  contract?: string;
}

function elem(id: string, label: string, bytes: number): [string, Record<string, unknown>] {
  return [id, { label, encoding: 'inplace', numberOfBytes: String(bytes) }];
}
function mapT(
  id: string,
  keyId: string,
  valId: string,
): [string, Record<string, unknown>] {
  return [
    id,
    {
      label: `mapping`,
      encoding: 'mapping',
      numberOfBytes: '32',
      key: keyId,
      value: valId,
    },
  ];
}
function farrT(
  id: string,
  base: string,
  len: number,
  bytes: number,
): [string, Record<string, unknown>] {
  return [
    id,
    { label: '', encoding: 'inplace', numberOfBytes: String(bytes), base, length: String(len) },
  ];
}
function darrT(id: string, base: string): [string, Record<string, unknown>] {
  return [id, { label: '', encoding: 'dynamic_array', numberOfBytes: '32', base }];
}
function structT(
  id: string,
  label: string,
  bytes: number,
  members: { label: string; slot: string; offset: number; type: string }[],
): [string, Record<string, unknown>] {
  return [id, { label: `struct ${label}`, encoding: 'inplace', numberOfBytes: String(bytes), members }];
}

function layout(
  fields: FieldOpts[],
  types: [string, Record<string, unknown>][],
): SolcStorageLayout {
  return {
    storage: fields.map((f) => ({
      label: f.name,
      slot: String(f.slot ?? 0),
      offset: f.offset ?? 0,
      type: f.type ?? 't_uint256',
      ...(f.contract ? { contract: f.contract } : {}),
    })),
    types: Object.fromEntries(types) as SolcStorageLayout['types'],
  };
}

const UINT256 = elem('t_uint256', 'uint256', 32);
const UINT128 = elem('t_uint128', 'uint128', 16);
const UINT64 = elem('t_uint64', 'uint64', 8);
const ADDRESS = elem('t_address', 'address', 20);
const BOOL = elem('t_bool', 'bool', 1);
const BYTES32 = elem('t_bytes32', 'bytes32', 32);

function kinds(findings: Finding[]): FindingKind[] {
  return findings.map((f) => f.kind);
}

/* -------------------------------- 测试 ---------------------------------- */

describe('根区域：追加规则', () => {
  it('末端整 slot 追加 -> compatible + appended', () => {
    const o = layout(
      [
        { name: 'owner', type: 't_address', slot: 0, offset: 0 },
        { name: 'x', type: 't_uint256', slot: 1, offset: 0 },
      ],
      [UINT256, ADDRESS],
    );
    const n = layout(
      [
        { name: 'owner', type: 't_address', slot: 0, offset: 0 },
        { name: 'x', type: 't_uint256', slot: 1, offset: 0 },
        { name: 'y', type: 't_uint256', slot: 2, offset: 0 },
      ],
      [UINT256, ADDRESS],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'compatible');
    assert.ok(kinds(r.findings).includes('appended'));
    const f = r.findings.find((x) => x.kind === 'appended')!;
    assert.deepEqual(f.path, ['y']);
  });

  it('同一 slot 内打包追加（offset 16）-> compatible', () => {
    const o = layout([{ name: 'a', type: 't_uint128', slot: 0, offset: 0 }], [UINT128]);
    const n = layout(
      [
        { name: 'a', type: 't_uint128', slot: 0, offset: 0 },
        { name: 'b', type: 't_uint128', slot: 0, offset: 16 },
      ],
      [UINT128],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'compatible');
    assert.ok(kinds(r.findings).includes('appended'));
  });

  it('中间插入导致后续字段移动 -> incompatible(field-inserted + field-moved)', () => {
    const o = layout(
      [
        { name: 'a', type: 't_uint64', slot: 0, offset: 0 },
        { name: 'b', type: 't_uint64', slot: 0, offset: 8 },
      ],
      [UINT64],
    );
    const n = layout(
      [
        { name: 'a', type: 't_uint64', slot: 0, offset: 0 },
        { name: 'c', type: 't_uint64', slot: 0, offset: 8 },
        { name: 'b', type: 't_uint64', slot: 0, offset: 16 },
      ],
      [UINT64],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    assert.ok(kinds(r.findings).includes('field-moved'));
    assert.ok(kinds(r.findings).includes('field-inserted'));
    assert.deepEqual(
      r.findings.find((f) => f.kind === 'field-moved')!.path,
      ['b'],
    );
  });

  it('删除已有字段 -> field-removed', () => {
    const o = layout(
      [
        { name: 'a', type: 't_bool', slot: 0, offset: 0 },
        { name: 'b', type: 't_uint256', slot: 1 },
      ],
      [BOOL, UINT256],
    );
    const n = layout([{ name: 'a', type: 't_bool', slot: 0, offset: 0 }], [BOOL]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    assert.ok(kinds(r.findings).includes('field-removed'));
  });
});

describe('gap 规则', () => {
  const gapT = (len: number) => farrT(`t_array(t_uint256)${len}_storage`, 't_uint256', len, len * 32);

  it('gap 原地缩小 1 个 slot 承接新字段 -> compatible', () => {
    const o = layout(
      [
        { name: 'x', slot: 0 },
        { name: '__gap', type: gapT(49)[0], slot: 1 },
      ],
      [UINT256, gapT(49)],
    );
    const n = layout(
      [
        { name: 'x', slot: 0 },
        { name: 'y', slot: 1 },
        { name: '__gap', type: gapT(48)[0], slot: 2 },
      ],
      [UINT256, gapT(48)],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'compatible');
    assert.ok(kinds(r.findings).includes('gap-reduced'));
    assert.ok(kinds(r.findings).includes('appended'));
  });

  it('gap 整体清空为 0 长（游标移动到尾部）-> compatible', () => {
    const o = layout(
      [
        { name: 'x', slot: 0 },
        { name: '__gap', type: gapT(2)[0], slot: 1 },
      ],
      [UINT256, gapT(2)],
    );
    const n = layout(
      [
        { name: 'x', slot: 0 },
        { name: 'y', slot: 1 },
        { name: 'z', slot: 2 },
        { name: '__gap', type: gapT(0)[0], slot: 3 },
      ],
      [UINT256, gapT(0)],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'compatible', JSON.stringify(r.findings, null, 2));
  });

  it('gap 移动（起点前移/扩张）-> gap-moved error', () => {
    const o = layout(
      [
        { name: 'x', slot: 0 },
        { name: '__gap', type: gapT(3)[0], slot: 1 },
      ],
      [UINT256, gapT(3)],
    );
    const n = layout(
      [
        { name: 'x', slot: 0 },
        { name: 'y', slot: 1 },
        // 新 gap[3] 起点 slot2 但仍想占 3 个 slot -> 扩张越过旧末端 slot4
        { name: '__gap', type: gapT(3)[0], slot: 2 },
      ],
      [UINT256, gapT(3)],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    assert.ok(kinds(r.findings).includes('gap-moved'));
  });

  it('gap 储备耗尽且新字段越过旧末端 -> gap-overflow warning, unknown', () => {
    const o = layout(
      [
        { name: 'x', slot: 0 },
        { name: '__gap', type: gapT(2)[0], slot: 1 },
      ],
      [UINT256, gapT(2)],
    );
    const n = layout(
      [
        { name: 'x', slot: 0 },
        { name: 'y', slot: 1 },
        { name: 'z', slot: 2 },
        { name: 'w', slot: 3 },
        { name: '__gap', type: gapT(0)[0], slot: 4 },
      ],
      [UINT256, gapT(0)],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'unknown');
    assert.ok(kinds(r.findings).includes('gap-overflow'));
  });

  it('gap 被整体删除 -> gap-removed warning（即使区间被精确承接）', () => {
    const o = layout(
      [
        { name: 'x', slot: 0 },
        { name: '__gap', type: gapT(2)[0], slot: 1 },
      ],
      [UINT256, gapT(2)],
    );
    const n = layout(
      [
        { name: 'x', slot: 0 },
        { name: 'y', slot: 1 },
        { name: 'z', slot: 2 },
      ],
      [UINT256],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'unknown');
    assert.ok(kinds(r.findings).includes('gap-removed'));
  });

  it('自定义 gap 名（--gap 正则）也被识别', () => {
    const mygap = farrT('t_array(t_uint256)5_storage', 't_uint256', 5, 160);
    const o = layout(
      [
        { name: 'x', slot: 0 },
        { name: 'reservedSpace', type: mygap[0], slot: 1 },
      ],
      [UINT256, mygap],
    );
    const n = layout(
      [
        { name: 'x', slot: 0 },
        { name: 'y', slot: 1 },
        { name: 'reservedSpace', type: farrT('t_array(t_uint256)4_storage', 't_uint256', 4, 128)[0], slot: 2 },
      ],
      [UINT256, farrT('t_array(t_uint256)4_storage', 't_uint256', 4, 128)],
    );
    const r = compareLayouts(o, n, { gapNamePattern: /^reservedSpace$/ });
    assert.equal(r.verdict, 'compatible');
  });
});

describe('基本类型变化', () => {
  it('uint128 -> uint256 位宽变化', () => {
    const o = layout([{ name: 'a', type: 't_uint128' }], [UINT128]);
    const n = layout([{ name: 'a', type: 't_uint256' }], [UINT256]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    assert.ok(kinds(r.findings).includes('width-changed'));
    // 证据包含旧/新 numberOfBytes
    const f = r.findings.find((x) => x.kind === 'width-changed')!;
    assert.equal(f.oldEvidence!.bytes, '16');
    assert.equal(f.newEvidence!.bytes, '32');
  });

  it('bytes32 -> uint256 同宽不同种类 -> type-changed', () => {
    const o = layout([{ name: 'h', type: 't_bytes32' }], [BYTES32]);
    const n = layout([{ name: 'h', type: 't_uint256' }], [UINT256]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    assert.ok(kinds(r.findings).includes('type-changed'));
  });

  it('enum 底层宽度 1 -> 2 -> enum-width-changed', () => {
    const e1: [string, Record<string, unknown>] = [
      't_enum(S)',
      { label: 'S', encoding: 'inplace', numberOfBytes: '1' },
    ];
    const e2: [string, Record<string, unknown>] = [
      't_enum(S)',
      { label: 'S', encoding: 'inplace', numberOfBytes: '2' },
    ];
    const o = layout([{ name: 's', type: 't_enum(S)' }], [e1]);
    const n = layout([{ name: 's', type: 't_enum(S)' }], [e2]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    assert.ok(kinds(r.findings).includes('enum-width-changed'));
  });

  it('仅类型重命名且结构一致 -> renamed info', () => {
    const t1 = structT('t_struct(A)x', 'A', 32, [
      { label: 'x', slot: '0', offset: 0, type: 't_uint256' },
    ]);
    const t2 = structT('t_struct(B)y', 'B', 32, [
      { label: 'x', slot: '0', offset: 0, type: 't_uint256' },
    ]);
    const o = layout([{ name: 's', type: 't_struct(A)x' }], [UINT256, t1]);
    const n = layout([{ name: 's', type: 't_struct(B)y' }], [UINT256, t2]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'compatible');
    assert.ok(kinds(r.findings).includes('renamed'));
  });
});

describe('数组与 mapping', () => {
  it('定长数组长度变化 -> array-length-changed', () => {
    const t3 = farrT('t_array(t_uint256)3_storage', 't_uint256', 3, 96);
    const t4 = farrT('t_array(t_uint256)4_storage', 't_uint256', 4, 128);
    const o = layout([{ name: 'v', type: t3[0] }], [UINT256, t3]);
    const n = layout([{ name: 'v', type: t4[0] }], [UINT256, t4]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    assert.ok(kinds(r.findings).includes('array-length-changed'));
  });

  it('定长数组元素位宽变化 -> array-base-changed，路径含 [*]', () => {
    // 等长（都为 4），元素 uint64(8B) -> uint128(16B)，总长 32 -> 64
    const a = farrT('t_array(t_uint64)4_storage', 't_uint64', 4, 32);
    const b = farrT('t_array(t_uint128)4_storage', 't_uint128', 4, 64);
    const o = layout([{ name: 'v', type: a[0] }], [UINT64, a]);
    const n = layout([{ name: 'v', type: b[0] }], [UINT128, b]);
    const r = compareLayouts(o, n);
    assert.ok(kinds(r.findings).includes('array-base-changed'));
    assert.ok(!kinds(r.findings).includes('array-length-changed'));
    const f = r.findings.find((x) => x.kind === 'array-base-changed')!;
    assert.deepEqual(f.path, ['v', '[*]']);
  });

  it('mapping 键变化 -> mapping-key-changed', () => {
    const ma = mapT('t_mapping(t_address=>t_uint256)', 't_address', 't_uint256');
    const mb = mapT('t_mapping(t_uint256=>t_uint256)', 't_uint256', 't_uint256');
    const o = layout([{ name: 'm', type: ma[0] }], [ADDRESS, UINT256, ma]);
    const n = layout([{ name: 'm', type: mb[0] }], [UINT256, mb]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    assert.ok(kinds(r.findings).includes('mapping-key-changed'));
    assert.deepEqual(r.findings.find((x) => x.kind === 'mapping-key-changed')!.path, ['m', '[key]']);
  });

  it('mapping 值结构体尾部追加 -> compatible（keccak 寻址区）', () => {
    const s1 = structT('t_struct(V)x', 'V', 32, [
      { label: 'a', slot: '0', offset: 0, type: 't_uint64' },
      { label: 'b', slot: '0', offset: 8, type: 't_uint64' },
    ]);
    const s2 = structT('t_struct(V)x', 'V', 64, [
      { label: 'a', slot: '0', offset: 0, type: 't_uint64' },
      { label: 'b', slot: '0', offset: 8, type: 't_uint64' },
      { label: 'c', slot: '1', offset: 0, type: 't_uint256' },
    ]);
    const m1 = mapT('t_mapping(t_address=>t_struct(V)x)', 't_address', 't_struct(V)x');
    const o = layout([{ name: 'm', type: m1[0] }], [ADDRESS, UINT256, UINT64, s1, m1]);
    const n = layout([{ name: 'm', type: m1[0] }], [ADDRESS, UINT256, UINT64, s2, m1]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'compatible', JSON.stringify(r.findings));
    assert.deepEqual(r.findings.find((x) => x.kind === 'appended')!.path, ['m', '[value]', 'c']);
  });

  it('变长数组元素结构体追加 -> compatible，路径含 []', () => {
    const s1 = structT('t_struct(E)x', 'E', 32, [
      { label: 'a', slot: '0', offset: 0, type: 't_uint256' },
    ]);
    const s2 = structT('t_struct(E)x', 'E', 64, [
      { label: 'a', slot: '0', offset: 0, type: 't_uint256' },
      { label: 'b', slot: '1', offset: 0, type: 't_uint256' },
    ]);
    const d1 = darrT('t_array(t_struct(E)x)dyn_storage', 't_struct(E)x');
    const o = layout([{ name: 'items', type: d1[0] }], [UINT256, s1, d1]);
    const n = layout([{ name: 'items', type: d1[0] }], [UINT256, s2, d1]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'compatible');
    assert.deepEqual(r.findings.find((x) => x.kind === 'appended')!.path, ['items', '[]', 'b']);
  });
});

describe('嵌套结构体', () => {
  const innerV1 = structT('t_struct(Inner)i', 'Inner', 32, [
    { label: 'a', slot: '0', offset: 0, type: 't_uint64' },
    { label: 'b', slot: '0', offset: 8, type: 't_uint64' },
  ]);

  it('末端结构体打包追加（复用最后 slot 剩余）-> compatible', () => {
    const innerV2 = structT('t_struct(Inner)i', 'Inner', 32, [
      { label: 'a', slot: '0', offset: 0, type: 't_uint64' },
      { label: 'b', slot: '0', offset: 8, type: 't_uint64' },
      { label: 'c', slot: '0', offset: 16, type: 't_uint128' },
    ]);
    const outer1 = structT('t_struct(Outer)o', 'Outer', 64, [
      { label: 'x', slot: '0', offset: 0, type: 't_uint256' },
      { label: 'inner', slot: '1', offset: 0, type: 't_struct(Inner)i' },
    ]);
    const outer2 = structT('t_struct(Outer)o', 'Outer', 64, [
      { label: 'x', slot: '0', offset: 0, type: 't_uint256' },
      { label: 'inner', slot: '1', offset: 0, type: 't_struct(Inner)i' },
    ]);
    const o = layout([{ name: 'outer', type: 't_struct(Outer)o' }], [UINT256, UINT64, UINT128, innerV1, outer1]);
    const n = layout([{ name: 'outer', type: 't_struct(Outer)o' }], [UINT256, UINT64, UINT128, innerV2, outer2]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'compatible', JSON.stringify(r.findings, null, 2));
    assert.deepEqual(r.findings.find((x) => x.kind === 'appended')!.path, ['outer', 'inner', 'c']);
  });

  it('非末端结构体增长导致后继成员移动 -> incompatible', () => {
    const innerV2 = structT('t_struct(Inner)i', 'Inner', 64, [
      { label: 'a', slot: '0', offset: 0, type: 't_uint64' },
      { label: 'b', slot: '0', offset: 8, type: 't_uint64' },
      { label: 'c', slot: '1', offset: 0, type: 't_uint256' },
    ]);
    // 旧 Outer: inner(slot0..31), x(slot32..63)。新 Inner 涨到 64B -> x 移到 64
    const outer1 = structT('t_struct(Outer)o', 'Outer', 64, [
      { label: 'inner', slot: '0', offset: 0, type: 't_struct(Inner)i' },
      { label: 'x', slot: '1', offset: 0, type: 't_uint256' },
    ]);
    const outer2 = structT('t_struct(Outer)o', 'Outer', 96, [
      { label: 'inner', slot: '0', offset: 0, type: 't_struct(Inner)i' },
      { label: 'x', slot: '2', offset: 0, type: 't_uint256' },
    ]);
    const o = layout([{ name: 'outer', type: 't_struct(Outer)o' }], [UINT256, UINT64, innerV1, outer1]);
    const n = layout([{ name: 'outer', type: 't_struct(Outer)o' }], [UINT256, UINT64, innerV2, outer2]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    assert.ok(kinds(r.findings).includes('struct-member-moved'));
    assert.deepEqual(r.findings.find((x) => x.kind === 'struct-member-moved')!.path, ['outer', 'x']);
  });

  it('结构体内 gap 缩小承接新成员 -> compatible', () => {
    const gap3 = farrT('t_array(t_uint256)3_storage', 't_uint256', 3, 96);
    const gap2 = farrT('t_array(t_uint256)2_storage', 't_uint256', 2, 64);
    const s1 = structT('t_struct(C)x', 'C', 128, [
      { label: 'a', slot: '0', offset: 0, type: 't_uint256' },
      { label: '__gap', slot: '1', offset: 0, type: gap3[0] },
    ]);
    const s2 = structT('t_struct(C)x', 'C', 128, [
      { label: 'a', slot: '0', offset: 0, type: 't_uint256' },
      { label: 'c', slot: '1', offset: 0, type: 't_uint256' },
      { label: '__gap', slot: '2', offset: 0, type: gap2[0] },
    ]);
    const o = layout([{ name: 'cfg', type: 't_struct(C)x' }], [UINT256, gap3, s1]);
    const n = layout([{ name: 'cfg', type: 't_struct(C)x' }], [UINT256, gap2, s2]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'compatible');
    assert.deepEqual(r.findings.find((x) => x.kind === 'appended')!.path, ['cfg', 'c']);
  });
});

describe('继承', () => {
  it('中间基类插入 -> inheritance-inserted + 后继字段 moved', () => {
    const o = layout(
      [
        { name: 'a', slot: 0, contract: 'Base' },
        { name: 'b', slot: 1, contract: 'Derived' },
      ],
      [UINT256],
    );
    const n = layout(
      [
        { name: 'a', slot: 0, contract: 'Base' },
        { name: 'm', slot: 1, contract: 'Middle' },
        { name: 'b', slot: 2, contract: 'Derived' },
      ],
      [UINT256],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    assert.ok(kinds(r.findings).includes('inheritance-inserted'));
    assert.ok(kinds(r.findings).includes('field-moved'));
  });

  it('相同合约集合但线性化顺序变化 -> inheritance-reordered', () => {
    const o = layout(
      [
        { name: 'a', slot: 0, contract: 'A' },
        { name: 'b', slot: 1, contract: 'B' },
      ],
      [UINT256],
    );
    const n = layout(
      [
        { name: 'b', slot: 0, contract: 'B' },
        { name: 'a', slot: 1, contract: 'A' },
      ],
      [UINT256],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    assert.ok(kinds(r.findings).includes('inheritance-reordered'));
  });

  it('缺少 contract 字段（旧编译器）-> 不做继承分析，仅按平坦布局比较', () => {
    const o = layout([{ name: 'a', slot: 0 }], [UINT256]);
    const n = layout(
      [
        { name: 'a', slot: 0 },
        { name: 'b', slot: 1 },
      ],
      [UINT256],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'compatible');
    assert.ok(!kinds(r.findings).includes('inheritance-inserted'));
  });
});

describe('未知类型保守处理', () => {
  it('保留字段出现未知编码 -> unknown-type warning，结论 unknown', () => {
    const o = layout(
      [
        { name: 'a', type: 't_uint256' },
        { name: 'b', type: 't_future(X)' },
      ],
      [
        UINT256,
        ['t_future(X)', { label: 'future', encoding: 'future_encoding', numberOfBytes: '32' }],
      ],
    );
    const n = o;
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'unknown');
    assert.ok(kinds(r.findings).includes('unknown-type'));
  });

  it('引用了 types 表中不存在的类型 id -> unknown', () => {
    const o = layout([{ name: 'a', type: 't_uint256' }], [UINT256]);
    const n = layout(
      [
        { name: 'a', type: 't_uint256' },
        { name: 'b', type: 't_missing', slot: 1 },
      ],
      [UINT256],
    );
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'unknown');
    assert.ok(kinds(r.findings).includes('unknown-type'));
  });

  it('unknown 不会被当作兼容（即使没有错误）', () => {
    const weird: [string, Record<string, unknown>] = [
      't_weird',
      { label: 'w', encoding: 'weird', numberOfBytes: '1' },
    ];
    const o = layout([{ name: 'a', type: 't_weird' }], [weird]);
    const n = layout([{ name: 'a', type: 't_weird' }], [weird]);
    const r = compareLayouts(o, n);
    assert.notEqual(r.verdict, 'compatible');
    assert.equal(r.verdict, 'unknown');
  });
});

describe('最短差异路径与证据', () => {
  it('深层 mapping->struct->array->struct 路径完整输出', () => {
    // mapping(address => V[]) where V { Inner[] items }，最深层改宽度
    const inner1 = structT('t_struct(In)x', 'In', 32, [
      { label: 'z', slot: '0', offset: 0, type: 't_uint64' },
    ]);
    const inner2 = structT('t_struct(In)x', 'In', 32, [
      { label: 'z', slot: '0', offset: 0, type: 't_uint128' },
    ]);
    const v1 = structT('t_struct(V)x', 'V', 32, [
      { label: 'items', slot: '0', offset: 0, type: 't_array(t_struct(In)x)dyn_storage' },
    ]);
    const v2 = v1;
    const arr = darrT('t_array(t_struct(In)x)dyn_storage', 't_struct(In)x');
    const varr = darrT('t_array(t_struct(V)x)dyn_storage', 't_struct(V)x');
    const m1 = mapT('t_mapping(t_address=>t_array(t_struct(V)x)dyn_storage)', 't_address', varr[0]);
    const common = [ADDRESS, UINT256, UINT64, UINT128, arr, varr, m1];
    const o = layout([{ name: 'm', type: m1[0] }], [...common, inner1, v1]);
    const n = layout([{ name: 'm', type: m1[0] }], [...common, inner2, v2]);
    const r = compareLayouts(o, n);
    assert.equal(r.verdict, 'incompatible');
    const f = r.findings.find((x) => x.kind === 'width-changed')!;
    assert.deepEqual(f.path, ['m', '[value]', '[]', 'items', '[]', 'z']);
    assert.ok(f.oldEvidence);
    assert.ok(f.newEvidence);
  });
});

describe('库入口与输入校验', () => {
  it('check() 接受带 storageLayout 的 artifact 包装', () => {
    const inner = { storageLayout: layout([{ name: 'a' }], [UINT256]) };
    const r = check(inner, {
      storageLayout: layout(
        [
          { name: 'a' },
          { name: 'b', slot: 1 },
        ],
        [UINT256],
      ),
    });
    assert.equal(r.verdict, 'compatible');
  });

  it('缺少 storage 数组 -> 抛错', () => {
    assert.throws(() =>
      compareLayouts({ types: {} } as unknown as SolcStorageLayout, {
        storage: [],
        types: {},
      }),
    );
  });

  it('slot 非整数字符串 -> 抛错', () => {
    assert.throws(() =>
      compareLayouts(
        { storage: [{ label: 'a', slot: '0x1', offset: 0, type: 't_uint256' }], types: {} },
        { storage: [], types: {} },
      ),
    );
  });
});

describe('样例文件对', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const manifest = JSON.parse(
    readFileSync(join(here, '..', 'samples', 'manifest.json'), 'utf8'),
  ) as Record<string, { expect: string }>;

  for (const [name, meta] of Object.entries(manifest)) {
    it(`${name} => ${meta.expect}`, () => {
      const dir = join(here, '..', 'samples', 'pairs', name);
      const o = JSON.parse(readFileSync(join(dir, 'v1.storageLayout.json'), 'utf8'));
      const n = JSON.parse(readFileSync(join(dir, 'v2.storageLayout.json'), 'utf8'));
      const r = compareLayouts(o, n);
      assert.equal(r.verdict, meta.expect, JSON.stringify(r.findings, null, 2));
    });
  }
});
