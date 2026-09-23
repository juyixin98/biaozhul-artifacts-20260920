/**
 * 把 solc storageLayout.types 解析为检查器内部使用的类型模型。
 *
 * 关键事实（依据 solc 文档与实际编译器输出）：
 *  - 基本类型编码为 inplace，numberOfBytes 为 1..32 的十进制字符串
 *  - 结构体 t_struct(...) encoding=inplace，带 members（相对 slot/offset）
 *  - mapping encoding=mapping，带 key / value
 *  - 定长数组 t_array(T)N_storage encoding=inplace，numberOfBytes=总长，
 *    带 base=T 与 length="N"
 *  - 变长数组 encoding=dynamic_array，带 base，numberOfBytes="32"
 *  - bytes / string encoding=bytes / string
 *  - enum: t_enum(...) encoding=inplace，numberOfBytes 为 1/2/...（按枚举成员数）
 *  - contract: t_contract(...) encoding=inplace，numberOfBytes=20
 *  - packed_array 为旧编译器/特殊编码
 */

import type { SolcStorageLayout, StorageLayoutType } from './types.js';

export type TypeKind =
  | 'elementary'
  | 'struct'
  | 'enum'
  | 'contract'
  | 'fixed-array'
  | 'dynamic-array'
  | 'mapping'
  | 'bytes'
  | 'string'
  | 'unknown';

export interface StructMemberModel {
  label: string;
  /** 相对结构体起点的字节偏移（member.slot*32 + member.offset） */
  byteOffset: number;
  rawSlot: string;
  rawOffset: number;
  typeId: string;
}

export interface TypeModel {
  typeId: string;
  label: string;
  encoding: string;
  numberOfBytes: number;
  kind: TypeKind;
  /** struct */
  members?: StructMemberModel[];
  /** mapping */
  keyTypeId?: string;
  valueTypeId?: string;
  /** array */
  baseTypeId?: string;
  length?: number;
  /** 原始定义（取证用） */
  raw: StorageLayoutType;
  /** 无法识别时给出原因 */
  unknownReason?: string;
}

export interface ParsedLayout {
  storage: SolcStorageLayout['storage'];
  types: Map<string, TypeModel>;
  raw: SolcStorageLayout;
}

function toIntNumber(value: string | undefined, field: string, typeId: string): number {
  if (value === undefined) throw new Error(`type ${typeId}: missing ${field}`);
  if (!/^\d+$/.test(value.trim())) {
    throw new Error(`type ${typeId}: ${field} is not a non-negative integer: ${value}`);
  }
  return Number(value.trim());
}

/**
 * solc 的 type 标识符语法（需要判型时使用，尽量只做保守的前缀匹配）：
 *   t_uint256, t_int128, t_bool, t_address, t_bytes20, t_bytes(变长无数字),
 *   t_string_storage, t_mapping(t_address=>t_struct(S)storage),
 *   t_array(t_uint256)3_storage, t_bytes_storage ...
 */
export function classify(t: StorageLayoutType, typeId: string): TypeKind {
  const enc = t.encoding;
  if (enc === 'mapping') return 'mapping';
  if (enc === 'dynamic_array') return 'dynamic-array';
  // 注意：solc 对 string 与 bytes 都使用 encoding "bytes"，先按类型标识符/标签识别 string
  if (
    enc === 'string' ||
    /^t_string(_storage|_dyn)?$/.test(typeId) ||
    t.label === 'string' ||
    t.label.startsWith('string ')
  ) {
    return 'string';
  }
  if (enc === 'bytes') return 'bytes';
  if (enc === 'packed_array') return 'dynamic-array';
  if (enc === 'inplace') {
    if (Array.isArray(t.members)) {
      return typeId.startsWith('t_struct(') || t.label.startsWith('struct ') ? 'struct' : 'struct';
    }
    if (typeId.startsWith('t_array(') || /^.*\[\d+\]$/.test(t.label)) return 'fixed-array';
    if (typeId.startsWith('t_enum(')) return 'enum';
    if (typeId.startsWith('t_contract(')) return 'contract';
    // 其余 inplace：基本值类型（uintN/intN/bool/address/bytesN/固定大小值类型）
    if (/^t_(u?int\d*|bool|address|bytes\d+|contract|function)/.test(typeId)) {
      return 'elementary';
    }
    // 有 members 已处理；没有 members 的 inplace 且名字陌生 -> 未知，保守处理
    return 'unknown';
  }
  return 'unknown';
}

export function parseType(typeId: string, t: StorageLayoutType): TypeModel {
  const model: TypeModel = {
    typeId,
    label: t.label,
    encoding: String(t.encoding),
    numberOfBytes: toIntNumber(t.numberOfBytes, 'numberOfBytes', typeId),
    kind: 'unknown',
    raw: t,
  };

  const kind = classify(t, typeId);
  model.kind = kind;

  if (kind === 'unknown') {
    model.unknownReason = `unsupported encoding/type identifier: encoding=${String(
      t.encoding,
    )}, id=${typeId}`;
    return model;
  }

  if (kind === 'struct') {
    if (!Array.isArray(t.members)) {
      model.kind = 'unknown';
      model.unknownReason = `struct ${typeId} has no members`;
      return model;
    }
    model.members = t.members.map((m) => ({
      label: m.label,
      byteOffset: toIntNumber(m.slot, 'slot', typeId) * 32 + m.offset,
      rawSlot: m.slot,
      rawOffset: m.offset,
      typeId: m.type,
    }));
  } else if (kind === 'mapping') {
    if (!t.key || !t.value) {
      model.kind = 'unknown';
      model.unknownReason = `mapping ${typeId} missing key/value`;
      return model;
    }
    model.keyTypeId = t.key;
    model.valueTypeId = t.value;
  } else if (kind === 'fixed-array') {
    if (!t.base) {
      model.kind = 'unknown';
      model.unknownReason = `array ${typeId} missing base`;
      return model;
    }
    model.baseTypeId = t.base;
    model.length = toIntNumber(t.length, 'length', typeId);
  } else if (kind === 'dynamic-array') {
    if (!t.base) {
      model.kind = 'unknown';
      model.unknownReason = `dynamic array ${typeId} missing base`;
      return model;
    }
    model.baseTypeId = t.base;
  }
  return model;
}

export function parseLayout(layout: SolcStorageLayout): ParsedLayout {
  if (!layout || typeof layout !== 'object') throw new Error('layout must be an object');
  if (!Array.isArray(layout.storage)) throw new Error('layout.storage must be an array');
  if (!layout.types || typeof layout.types !== 'object') {
    throw new Error('layout.types must be an object');
  }
  const types = new Map<string, TypeModel>();
  for (const [id, raw] of Object.entries(layout.types)) {
    try {
      types.set(id, parseType(id, raw));
    } catch (err) {
      // 单个类型解析失败 -> 记录为 unknown，而不是让整次检查崩溃
      types.set(id, {
        typeId: id,
        label: raw?.label ?? id,
        encoding: String(raw?.encoding ?? ''),
        numberOfBytes: Number.isFinite(Number(raw?.numberOfBytes))
          ? Number(raw.numberOfBytes)
          : NaN,
        kind: 'unknown',
        raw: raw as StorageLayoutType,
        unknownReason: (err as Error).message,
      });
    }
  }
  for (const item of layout.storage) {
    if (!item || typeof item.label !== 'string') throw new Error('storage item missing label');
    if (typeof item.offset !== 'number') throw new Error(`storage ${item.label}: offset not number`);
    if (typeof item.slot !== 'string' || !/^\d+$/.test(item.slot.trim())) {
      throw new Error(`storage ${item.label}: slot must be a decimal integer string`);
    }
  }
  return { storage: layout.storage, types, raw: layout };
}

/** 取类型模型；引用缺失时返回一个 unknown 模型而非抛异常 */
export function resolveType(types: Map<string, TypeModel>, typeId: string): TypeModel {
  const m = types.get(typeId);
  if (m) return m;
  return {
    typeId,
    label: typeId,
    encoding: '',
    numberOfBytes: NaN,
    kind: 'unknown',
    raw: { label: typeId, encoding: '', numberOfBytes: 'NaN' },
    unknownReason: `type id referenced but not defined in layout.types: ${typeId}`,
  };
}
