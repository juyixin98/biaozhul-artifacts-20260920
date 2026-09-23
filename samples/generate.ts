/**
 * 样例生成器。
 *
 * 用与 solc 一致的存储打包规则（slot/offset 分配、结构体相对布局、
 * 定长数组尺寸、mapping/变长数组占一个 slot）从 Solidity 风格声明
 * 生成成对的 storageLayout JSON。输出形状与 solc 标准 JSON 的
 * contracts[*].storageLayout 字段完全一致，可直接作为检查器输入。
 *
 * 运行: npm run gen-samples
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/* ------------------------------ 迷你类型 DSL ----------------------------- */

type Ty =
  | { k: 'elem'; name: string; bytes: number }
  | { k: 'enum'; name: string; members: number }
  | { k: 'struct'; name: string; fields: { name: string; ty: Ty }[] }
  | { k: 'farr'; ty: Ty; len: number }
  | { k: 'darr'; ty: Ty }
  | { k: 'map'; key: Ty; val: Ty }
  | { k: 'raw'; name: 'string' | 'bytes' };

const u = (bits: number): Ty => ({ k: 'elem', name: `uint${bits}`, bytes: bits / 8 });
const addr = (): Ty => ({ k: 'elem', name: 'address', bytes: 20 });
const bool = (): Ty => ({ k: 'elem', name: 'bool', bytes: 1 });
const bytesN = (n: number): Ty => ({ k: 'elem', name: `bytes${n}`, bytes: n });
const farr = (ty: Ty, len: number): Ty => ({ k: 'farr', ty, len });
const darr = (ty: Ty): Ty => ({ k: 'darr', ty });
const map = (key: Ty, val: Ty): Ty => ({ k: 'map', key, val });
const en = (name: string, members: number): Ty => ({ k: 'enum', name, members });
const str = (): Ty => ({ k: 'raw', name: 'string' });

interface VarDecl {
  name: string;
  ty: Ty;
  /** 继承场景下该变量所属合约名 */
  contract?: string;
}

/* ------------------------------ solc 风格标识 ----------------------------- */

let enumCounter = 0;
const enumBytes = (members: number) => (members <= 256 ? 1 : members <= 65536 ? 2 : 4);

function typeId(ty: Ty, ctx: Ctx): string {
  switch (ty.k) {
    case 'elem':
      return `t_${ty.name}`;
    case 'enum': {
      const id = ctx.enumIds.get(ty.name);
      return `t_enum(${id ?? ty.name})`;
    }
    case 'struct':
      return `t_struct(${ty.name})${ctx.structSuffix(ty.name)}`;
    case 'farr':
      return `t_array(${typeId(ty.ty, ctx)})${ty.len}_storage`;
    case 'darr':
      return `t_array(${typeId(ty.ty, ctx)})dyn_storage`;
    case 'map':
      return `t_mapping(${typeId(ty.key, ctx)} => ${typeId(ty.val, ctx)})`;
    case 'raw':
      return ty.name === 'string' ? 't_string_storage' : 't_bytes_storage';
  }
}

function typeLabel(ty: Ty): string {
  switch (ty.k) {
    case 'elem':
      return ty.name;
    case 'enum':
      return ty.name;
    case 'struct':
      return `struct ${ty.name}`;
    case 'farr':
      return `${typeLabel(ty.ty)}[${ty.len}]`;
    case 'darr':
      return `${typeLabel(ty.ty)}[]`;
    case 'map':
      return `mapping(${typeLabel(ty.key)} => ${typeLabel(ty.val)})`;
    case 'raw':
      return ty.name;
  }
}

/* ------------------------------ 尺寸与编码 ------------------------------- */

interface SizeInfo {
  bytes: number; // numberOfBytes
  inplace: boolean;
}

function sizeOf(ty: Ty, ctx: Ctx): SizeInfo {
  switch (ty.k) {
    case 'elem':
      return { bytes: ty.bytes, inplace: true };
    case 'enum':
      return { bytes: enumBytes(ty.members), inplace: true };
    case 'raw':
      return { bytes: 32, inplace: false }; // string/bytes: encoding=bytes
    case 'map':
      return { bytes: 32, inplace: false };
    case 'darr':
      return { bytes: 32, inplace: false };
    case 'farr': {
      const e = sizeOf(ty.ty, ctx);
      return { bytes: e.bytes * ty.len, inplace: true };
    }
    case 'struct': {
      const slots = layoutStruct(ty, ctx);
      const maxSlot = slots.reduce((m, s) => Math.max(m, Number(s.slot)), 0);
      return { bytes: (maxSlot + 1) * 32, inplace: true };
    }
  }
}

/** solc 打包：返回成员相对结构体起点的 slot/offset */
function layoutStruct(structTy: Extract<Ty, { k: 'struct' }>, ctx: Ctx) {
  const out: { name: string; slot: string; offset: number; ty: Ty }[] = [];
  let slot = 0n;
  let offset = 0; // 已用字节（从 slot 低位起）
  for (const f of structTy.fields) {
    const s = sizeOf(f.ty, ctx);
    if (s.bytes === 0) {
      // 0 长定长数组：零字节，落在当前游标处且不推进（与 solc 一致）
      out.push({ name: f.name, slot: slot.toString(), offset, ty: f.ty });
      continue;
    }
    if (!s.inplace || s.bytes >= 32) {
      if (offset !== 0) {
        slot += 1n;
        offset = 0;
      }
      out.push({ name: f.name, slot: slot.toString(), offset: 0, ty: f.ty });
      if (s.bytes >= 32) slot += BigInt(s.bytes / 32);
      else slot += 1n;
    } else {
      if (offset + s.bytes > 32) {
        slot += 1n;
        offset = 0;
      }
      out.push({ name: f.name, slot: slot.toString(), offset, ty: f.ty });
      offset += s.bytes;
      if (offset === 32) {
        slot += 1n;
        offset = 0;
      }
    }
  }
  return out;
}

/* -------------------------------- 上下文 -------------------------------- */

interface Ctx {
  enumIds: Map<string, string>;
  structSuffix: (name: string) => string;
}

/* ------------------------------ 输出生成器 ------------------------------- */

interface StorageItemOut {
  label: string;
  offset: number;
  slot: string;
  type: string;
  contract?: string;
}
interface TypeOut {
  label: string;
  encoding: string;
  numberOfBytes: string;
  members?: { label: string; offset: number; slot: string; type: string }[];
  key?: string;
  value?: string;
  base?: string;
  length?: string;
}

function emit(vars: VarDecl[], opts: { enumSource?: string } = {}): {
  storage: StorageItemOut[];
  types: Record<string, TypeOut>;
} {
  const enumIds = new Map<string, string>();
  const sourceName = opts.enumSource ?? 'contracts/Sample.sol';
  // 收集 enum，按首次出现分配 solc 风格全限定 id
  let counter = 0;
  const walk = (ty: Ty): void => {
    if (ty.k === 'enum' && !enumIds.has(ty.name)) {
      enumIds.set(ty.name, `${sourceName}:${counter++}:${ty.name}`);
    }
    if (ty.k === 'struct') ty.fields.forEach((f) => walk(f.ty));
    if (ty.k === 'farr' || ty.k === 'darr') walk(ty.ty);
    if (ty.k === 'map') {
      walk(ty.key);
      walk(ty.val);
    }
  };
  vars.forEach((v) => walk(v.ty));

  const ctx: Ctx = {
    enumIds,
    structSuffix: (name) => `_storage_${sourceName.replaceAll('/', '_')}_${name}`,
  };

  // 顶层打包
  const storage: StorageItemOut[] = [];
  let slot = 0n;
  let offset = 0;
  for (const v of vars) {
    const s = sizeOf(v.ty, ctx);
    if (s.bytes === 0) {
      storage.push({
        label: v.name,
        offset,
        slot: slot.toString(),
        type: typeId(v.ty, ctx),
        ...(v.contract ? { contract: v.contract } : {}),
      });
      continue;
    }
    if (!s.inplace || s.bytes >= 32) {
      if (offset !== 0) {
        slot += 1n;
        offset = 0;
      }
      storage.push({
        label: v.name,
        offset: 0,
        slot: slot.toString(),
        type: typeId(v.ty, ctx),
        ...(v.contract ? { contract: v.contract } : {}),
      });
      slot += BigInt(s.bytes >= 32 ? s.bytes / 32 : 1);
    } else {
      if (offset + s.bytes > 32) {
        slot += 1n;
        offset = 0;
      }
      storage.push({
        label: v.name,
        offset,
        slot: slot.toString(),
        type: typeId(v.ty, ctx),
        ...(v.contract ? { contract: v.contract } : {}),
      });
      offset += s.bytes;
      if (offset === 32) {
        slot += 1n;
        offset = 0;
      }
    }
  }

  // 类型表：收集所有被引用类型
  const types = new Map<string, TypeOut>();
  const addType = (ty: Ty): void => {
    const id = typeId(ty, ctx);
    if (types.has(id)) return;
    const s = sizeOf(ty, ctx);
    switch (ty.k) {
      case 'elem':
        types.set(id, {
          label: ty.name,
          encoding: 'inplace',
          numberOfBytes: String(ty.bytes),
        });
        break;
      case 'enum':
        types.set(id, {
          label: ty.name,
          encoding: 'inplace',
          numberOfBytes: String(s.bytes),
        });
        break;
      case 'raw':
        types.set(id, { label: ty.name, encoding: 'bytes', numberOfBytes: '32' });
        break;
      case 'map':
        types.set(id, {
          label: typeLabel(ty),
          encoding: 'mapping',
          numberOfBytes: '32',
          key: typeId(ty.key, ctx),
          value: typeId(ty.val, ctx),
        });
        addType(ty.key);
        addType(ty.val);
        break;
      case 'darr':
        types.set(id, {
          label: typeLabel(ty),
          encoding: 'dynamic_array',
          numberOfBytes: '32',
          base: typeId(ty.ty, ctx),
        });
        addType(ty.ty);
        break;
      case 'farr':
        types.set(id, {
          label: typeLabel(ty),
          encoding: 'inplace',
          numberOfBytes: String(s.bytes),
          base: typeId(ty.ty, ctx),
          length: String(ty.len),
        });
        addType(ty.ty);
        break;
      case 'struct': {
        const members = layoutStruct(ty, ctx).map((m) => ({
          label: m.name,
          offset: m.offset,
          slot: m.slot,
          type: typeId(m.ty, ctx),
        }));
        types.set(id, {
          label: `struct ${ty.name}`,
          encoding: 'inplace',
          numberOfBytes: String(s.bytes),
          members,
        });
        ty.fields.forEach((f) => addType(f.ty));
        break;
      }
    }
  };
  vars.forEach((v) => addType(v.ty));

  return { storage, types: Object.fromEntries(types) };
}

/* ------------------------------ 样例定义 -------------------------------- */

const Inner = (): Ty => ({
  k: 'struct',
  name: 'Inner',
  fields: [
    { name: 'a', ty: u(64) },
    { name: 'b', ty: u(64) },
  ],
});
const OuterWith = (inner: Ty, tail: boolean): Ty => ({
  k: 'struct',
  name: 'Outer',
  fields: tail
    ? [
        { name: 'x', ty: u(256) },
        { name: 'inner', ty: inner },
      ]
    : [
        { name: 'inner', ty: inner },
        { name: 'x', ty: u(256) },
      ],
});

const samples: Record<
  string,
  {
    old: ReturnType<typeof emit>;
    neu: ReturnType<typeof emit>;
    sol: string;
    expect: string;
  }
> = {
  '01-safe-append': {
    expect: 'compatible',
    sol: `// V1: { address owner; uint256 x; }
// V2: 追加 uint256 y（末端追加，安全）`,
    old: emit([
      { name: 'owner', ty: addr() },
      { name: 'x', ty: u(256) },
    ]),
    neu: emit([
      { name: 'owner', ty: addr() },
      { name: 'x', ty: u(256) },
      { name: 'y', ty: u(256) },
    ]),
  },

  '02-packed-safe': {
    expect: 'compatible',
    sol: `// V1: uint128 a;  V2: uint128 a; uint128 b; —— b 装入同一 slot 剩余 16 字节`,
    old: emit([{ name: 'a', ty: u(128) }]),
    neu: emit([
      { name: 'a', ty: u(128) },
      { name: 'b', ty: u(128) },
    ]),
  },

  '03-packed-insert': {
    expect: 'incompatible',
    sol: `// V1: uint64 a,b 打包在 slot0; V2 在中间插入 c，b 的 offset 8->16`,
    old: emit([
      { name: 'a', ty: u(64) },
      { name: 'b', ty: u(64) },
    ]),
    neu: emit([
      { name: 'a', ty: u(64) },
      { name: 'c', ty: u(64) },
      { name: 'b', ty: u(64) },
    ]),
  },

  '04-gap-shrink': {
    expect: 'compatible',
    sol: `// OpenZeppelin 模式：__gap 原地缩小 1，释放 slot 承接新变量 y
// V1: uint256 x; uint256[49] __gap;
// V2: uint256 x; uint256 y; uint256[48] __gap;`,
    old: emit([
      { name: 'x', ty: u(256) },
      { name: '__gap', ty: farr(u(256), 49) },
    ]),
    neu: emit([
      { name: 'x', ty: u(256) },
      { name: 'y', ty: u(256) },
      { name: '__gap', ty: farr(u(256), 48) },
    ]),
  },

  '05-gap-partial-overflow': {
    expect: 'unknown',
    sol: `// gap 只有 2 个 slot：y,z 吃掉储备，w 越过旧区域末端。
// 平坦追加本身可工作，但 gap 储备已耗尽 -> gap-overflow 警告（结论 unknown）。
// V1: uint256 x; uint256[2] __gap;
// V2: uint256 x,y,z,w; uint256[0] __gap;（声明顺序 x,y,z,w,__gap）`,
    old: emit([
      { name: 'x', ty: u(256) },
      { name: '__gap', ty: farr(u(256), 2) },
    ]),
    neu: emit([
      { name: 'x', ty: u(256) },
      { name: 'y', ty: u(256) },
      { name: 'z', ty: u(256) },
      { name: 'w', ty: u(256) },
      { name: '__gap', ty: farr(u(256), 0) },
    ]),
  },

  '06-gap-exact-boundary': {
    expect: 'unknown',
    sol: `// gap 边界但安全标记消失：旧 __gap[2] 在新版本中被整体删除，
// y,z 虽然精确落在释放区间，但检查器无法确认“删除 gap”是有意为之，
// 报 gap-removed 警告，结论 unknown（推荐写法见 06b：保留同名 gap 缩到 0）。`,
    old: emit([
      { name: 'x', ty: u(256) },
      { name: '__gap', ty: farr(u(256), 2) },
    ]),
    neu: emit([
      { name: 'x', ty: u(256) },
      { name: 'y', ty: u(256) },
      { name: 'z', ty: u(256) },
    ]),
  },

  '06b-gap-shrink-to-zero': {
    expect: 'compatible',
    sol: `// 纯兼容边界：__gap 同标签由 2 缩到 0，y,z 精确填满释放区间`,
    old: emit([
      { name: 'x', ty: u(256) },
      { name: '__gap', ty: farr(u(256), 2) },
    ]),
    neu: emit([
      { name: 'x', ty: u(256) },
      { name: 'y', ty: u(256) },
      { name: 'z', ty: u(256) },
      { name: '__gap', ty: farr(u(256), 0) },
    ]),
  },

  '07-nested-struct-tail': {
    expect: 'compatible',
    sol: `// Inner 位于 Outer 末端：给 Inner 尾部追加 c 不改变任何既有成员位置
// 且 Outer 是合约末端变量，整体追加安全`,
    old: emit([{ name: 'outer', ty: OuterWith(Inner(), true) }]),
    neu: emit([
      {
        name: 'outer',
        ty: OuterWith(
          {
            k: 'struct',
            name: 'Inner',
            fields: [
              { name: 'a', ty: u(64) },
              { name: 'b', ty: u(64) },
              { name: 'c', ty: u(128) }, // 装入同 slot
            ],
          },
          true,
        ),
      },
    ]),
  },

  '08-nested-struct-mid': {
    expect: 'incompatible',
    sol: `// Inner 后面还有 Outer.x：给 Inner 尾部追加 c（新 slot）会把 Outer.x 整体后移 1 slot`,
    old: emit([{ name: 'outer', ty: OuterWith(Inner(), false) }]),
    neu: emit([
      {
        name: 'outer',
        ty: OuterWith(
          {
            k: 'struct',
            name: 'Inner',
            fields: [
              { name: 'a', ty: u(64) },
              { name: 'b', ty: u(64) },
              { name: 'c', ty: u(256) },
            ],
          },
          false,
        ),
      },
    ]),
  },

  '09-mapping-value-struct-append': {
    expect: 'compatible',
    sol: `// mapping 值由 keccak(key,slot) 独立寻址：值结构体尾部追加总是安全`,
    old: emit([{ name: 'data', ty: map(addr(), Inner()) }]),
    neu: emit([
      {
        name: 'data',
        ty: map(
          addr(),
          {
            k: 'struct',
            name: 'Inner',
            fields: [
              { name: 'a', ty: u(64) },
              { name: 'b', ty: u(64) },
              { name: 'c', ty: u(256) },
            ],
          },
        ),
      },
    ]),
  },

  '10-dynarray-element-append': {
    expect: 'compatible',
    sol: `// 变长数组元素位于 keccak(slot) 区：元素结构体追加成员安全`,
    old: emit([{ name: 'items', ty: darr(Inner()) }]),
    neu: emit([
      {
        name: 'items',
        ty: darr({
          k: 'struct',
          name: 'Inner',
          fields: [
            { name: 'a', ty: u(64) },
            { name: 'b', ty: u(64) },
            { name: 'c', ty: u(128) },
          ],
        }),
      },
    ]),
  },

  '11-fixed-array-length': {
    expect: 'incompatible',
    sol: `// 定长数组长度变化 3 -> 4`,
    old: emit([{ name: 'vals', ty: farr(u(256), 3) }]),
    neu: emit([{ name: 'vals', ty: farr(u(256), 4) }]),
  },

  '12-mapping-key-change': {
    expect: 'incompatible',
    sol: `// mapping 键 address -> uint256：槽位派生完全不同`,
    old: emit([{ name: 'm', ty: map(addr(), u(256)) }]),
    neu: emit([{ name: 'm', ty: map(u(256), u(256)) }]),
  },

  '13-width-change': {
    expect: 'incompatible',
    sol: `// uint128 -> uint256 位宽变化`,
    old: emit([{ name: 'a', ty: u(128) }]),
    neu: emit([{ name: 'a', ty: u(256) }]),
  },

  '14-enum-width': {
    expect: 'incompatible',
    sol: `// enum 成员数从 2 增到 300：底层宽度 1B -> 2B`,
    old: emit([{ name: 'state', ty: en('State', 2) }]),
    neu: emit([{ name: 'state', ty: en('State', 300) }]),
  },

  '15-field-removed': {
    expect: 'incompatible',
    sol: `// 删除中间字段 x（y 虽位置未变，x 本身删除即破坏性变化）`,
    old: emit([
      { name: 'owner', ty: addr() },
      { name: 'x', ty: bool() },
      { name: 'y', ty: u(256) },
    ]),
    neu: emit([
      { name: 'owner', ty: addr() },
      { name: 'y', ty: u(256) },
    ]),
  },

  '16-inheritance-insert': {
    expect: 'incompatible',
    sol: `// Base: uint256 a; Derived is Base + uint256 b
// V2 在 Base 与 Derived 之间插入 Middle(uint256 m)，b 被推移 1 slot`,
    old: (() => {
      const out = emit([
        { name: 'a', ty: u(256), contract: 'Base' },
        { name: 'b', ty: u(256), contract: 'Derived' },
      ]);
      return out;
    })(),
    neu: emit([
      { name: 'a', ty: u(256), contract: 'Base' },
      { name: 'm', ty: u(256), contract: 'Middle' },
      { name: 'b', ty: u(256), contract: 'Derived' },
    ]),
  },

  '17-gap-in-struct': {
    expect: 'compatible',
    sol: `// 结构体内 __gap 原地缩小 1 个 slot，承接尾部新成员 c（末端结构体，可增长）
// V1: struct Cfg { uint256 a; uint256[3] __gap; }
// V2: struct Cfg { uint256 a; uint256 c; uint256[2] __gap; }`,
    old: emit([
      {
        name: 'cfg',
        ty: {
          k: 'struct',
          name: 'Cfg',
          fields: [
            { name: 'a', ty: u(256) },
            { name: '__gap', ty: farr(u(256), 3) },
          ],
        },
      },
    ]),
    neu: emit([
      {
        name: 'cfg',
        ty: {
          k: 'struct',
          name: 'Cfg',
          fields: [
            { name: 'a', ty: u(256) },
            { name: 'c', ty: u(256) },
            { name: '__gap', ty: farr(u(256), 2) },
          ],
        },
      },
    ]),
  },

  '18-unknown-type': {
    expect: 'unknown',
    sol: `// V2 的 t_futuristic 使用编译器未来编码 future_encoding，检查器无法判定`,
    old: emit([{ name: 'a', ty: u(256) }]),
    neu: (() => {
      const out = emit([
        { name: 'a', ty: u(256) },
        { name: 'b', ty: bytesN(32) },
      ]);
      // 人为构造未知编码
      out.types['t_futuristic(P)'] = {
        label: 'futuristic',
        encoding: 'future_encoding',
        numberOfBytes: '32',
      };
      const item = out.storage.find((s) => s.label === 'b')!;
      item.type = 't_futuristic(P)';
      delete out.types['t_bytes32'];
      return out;
    })(),
  },

  '19-type-swap': {
    expect: 'incompatible',
    sol: `// 同宽不同种类：bytes32 -> uint256`,
    old: emit([{ name: 'h', ty: bytesN(32) }]),
    neu: emit([{ name: 'h', ty: u(256) }]),
  },

  '20-string-bytes-ok': {
    expect: 'compatible',
    sol: `// string 保持 string，bytes 保持 bytes（同编码稳定）`,
    old: emit([
      { name: 'name', ty: str() },
      { name: 'blob', ty: { k: 'raw', name: 'bytes' } },
    ]),
    neu: emit([
      { name: 'name', ty: str() },
      { name: 'blob', ty: { k: 'raw', name: 'bytes' } },
    ]),
  },
};

/* ------------------------------ 写文件 ---------------------------------- */

const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, '..', 'samples', 'pairs');

let manifest: Record<string, { expect: string; sol: string; files: string[] }> = {};
for (const [name, s] of Object.entries(samples)) {
  const dir = join(outDir, name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, 'v1.storageLayout.json'), JSON.stringify(s.old, null, 2) + '\n');
  writeFileSync(join(dir, 'v2.storageLayout.json'), JSON.stringify(s.neu, null, 2) + '\n');
  writeFileSync(join(dir, 'contracts.sol'), `// SPDX-License-Identifier: MIT\npragma solidity ^0.8.20;\n\n${s.sol}\n`);
  manifest[name] = {
    expect: s.expect,
    sol: s.sol,
    files: ['v1.storageLayout.json', 'v2.storageLayout.json', 'contracts.sol'],
  };
}
mkdirSync(join(here, '..', 'samples'), { recursive: true });
writeFileSync(join(here, '..', 'samples', 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(`generated ${Object.keys(samples).length} sample pairs -> ${outDir}`);
