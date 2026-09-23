/**
 * 存储布局兼容比较引擎。
 *
 * 比较在“区域（region）”上递归进行：
 *   - 根区域：合约的 storage[]（绝对 slot，从 slot 0 开始）
 *   - 结构体区域：types[t_struct].members（相对字节偏移）
 *
 * 记账规则（所有位置用 bigint 字节数，兼容超长定长数组）：
 *   1. 同名保留字段的起点（slot*32+offset）必须不变，类型递归兼容；
 *      起点变化即 field-moved / struct-member-moved（错误）。
 *   2. __gap 定长数组只允许“原地缩小”：新区间必须是旧区间的子集；
 *      缩小释放出的字节加入 freed interval 并记 gap-reduced。
 *   3. 新字段必须落在 (a) gap 释放区间内，或 (b) 旧区域末端之后的追加区。
 *      落到保留字段位置 -> field-inserted 错误；超过 gap 储备 -> gap-overflow 警告。
 *   4. 穿过 mapping / 变长数组后，存储由 keccak 寻址、彼此独立，
 *      因此其内部结构体允许尾部追加（relocatable）。
 *   5. 遇到未知类型：unknown-type，结论为 unknown，绝不默认安全。
 */

import type {
  CheckOptions,
  CompatReport,
  Finding,
  FindingKind,
  Severity,
  SolcStorageLayout,
} from './types.js';
import { parseLayout, resolveType, type ParsedLayout, type TypeModel } from './typeModel.js';

interface Interval {
  start: bigint;
  end: bigint;
}

interface RegionEntry {
  label: string;
  start: bigint;
  end: bigint;
  typeId: string;
  isGap: boolean;
  contract?: string;
  raw: { slot: string; offset: number; type: string };
}

interface Ctx {
  /** 已穿过 mapping/动态数组：内层结构体尾部追加安全 */
  relocatable: boolean;
  /** 本区域末端是否可自由增长（根区域恒真；结构体仅当其位于父区域末端时真） */
  tail: boolean;
  /** 直接上层类型角色，用于把通用问题改写成 mapping/array 专用 kind */
  role: 'root' | 'member' | 'key' | 'value' | 'base';
}

const ROOT_CTX: Ctx = { relocatable: false, tail: true, role: 'root' };

class FindingBuilder {
  readonly findings: Finding[] = [];

  add(
    kind: FindingKind,
    severity: Severity,
    path: string[],
    message: string,
    oldEvidence?: Record<string, unknown>,
    newEvidence?: Record<string, unknown>,
  ): void {
    this.findings.push({
      kind,
      severity,
      path,
      message,
      ...(oldEvidence ? { oldEvidence } : {}),
      ...(newEvidence ? { newEvidence } : {}),
    });
  }
}

/* ------------------------------ 区间运算 ------------------------------- */

function unionSorted(intervals: Interval[]): Interval[] {
  if (intervals.length === 0) return [];
  const sorted = [...intervals].sort((a, b) => (a.start < b.start ? -1 : 1));
  const out: Interval[] = [{ ...sorted[0]! }];
  for (let i = 1; i < sorted.length; i++) {
    const iv = sorted[i]!;
    const last = out[out.length - 1]!;
    if (iv.start <= last.end) {
      if (iv.end > last.end) last.end = iv.end;
    } else {
      out.push({ ...iv });
    }
  }
  return out;
}

/** 返回 target 相对 covered 区间集未被覆盖的部分（区间可能被切成两段） */
function uncovered(target: Interval, covered: Interval[]): Interval[] {
  let parts: Interval[] = [target];
  for (const c of covered) {
    const next: Interval[] = [];
    for (const p of parts) {
      if (c.end <= p.start || c.start >= p.end) {
        next.push(p);
      } else {
        if (c.start > p.start) next.push({ start: p.start, end: c.start });
        if (c.end < p.end) next.push({ start: c.end, end: p.end });
      }
    }
    parts = next;
  }
  return parts.filter((p) => p.end > p.start);
}

/* ------------------------------ 入口构建 ------------------------------- */

function slotStartBig(slot: string, offset: number): bigint {
  return BigInt(slot.trim()) * 32n + BigInt(offset);
}

/** 一个 in-place 类型占用的字节数（mapping/动态数组/bytes/string 占一个 slot=32） */
function inplaceSpanBig(m: TypeModel): bigint {
  if (Number.isNaN(m.numberOfBytes)) return 32n;
  return BigInt(m.raw.numberOfBytes.trim());
}

function isGapType(m: TypeModel): boolean {
  return m.kind === 'fixed-array';
}

function buildRootEntries(layout: ParsedLayout, gapRe: RegExp): RegionEntry[] {
  return layout.storage.map((item) => {
    const model = resolveType(layout.types, item.type);
    const start = slotStartBig(item.slot, item.offset);
    return {
      label: item.label,
      start,
      end: start + inplaceSpanBig(model),
      typeId: item.type,
      isGap: gapRe.test(item.label) && isGapType(model),
      contract: item.contract,
      raw: { slot: item.slot, offset: item.offset, type: item.type },
    };
  });
}

function buildStructEntries(
  owner: TypeModel,
  types: Map<string, TypeModel>,
  gapRe: RegExp,
): RegionEntry[] {
  return (owner.members ?? []).map((mem) => {
    const model = resolveType(types, mem.typeId);
    const start = BigInt(mem.rawSlot.trim()) * 32n + BigInt(mem.rawOffset);
    return {
      label: mem.label,
      start,
      end: start + inplaceSpanBig(model),
      typeId: mem.typeId,
      isGap: gapRe.test(mem.label) && isGapType(model),
      raw: { slot: mem.rawSlot, offset: mem.rawOffset, type: mem.typeId },
    };
  });
}

/* ------------------------------ 主比较逻辑 ----------------------------- */

interface LayoutPair {
  old: ParsedLayout;
  new: ParsedLayout;
}

export function compareLayouts(
  oldLayout: SolcStorageLayout,
  newLayout: SolcStorageLayout,
  options: CheckOptions = {},
): CompatReport {
  const gapRe = options.gapNamePattern ?? /^__gap(?:_[A-Za-z0-9]+)*$/;
  const pair: LayoutPair = { old: parseLayout(oldLayout), new: parseLayout(newLayout) };
  const fb = new FindingBuilder();

  const oldEntries = buildRootEntries(pair.old, gapRe);
  const newEntries = buildRootEntries(pair.new, gapRe);

  compareRegion(oldEntries, newEntries, [], ROOT_CTX, pair, fb, gapRe);
  analyzeInheritance(oldEntries, newEntries, pair, fb);

  return finalize(fb);
}

function finalize(fb: FindingBuilder): CompatReport {
  const findings = [...fb.findings].sort((a, b) => {
    const pa = a.path.join('.');
    const pb = b.path.join('.');
    return pa < pb ? -1 : pa > pb ? 1 : a.kind.localeCompare(b.kind);
  });
  const errors = findings.filter((f) => f.severity === 'error').length;
  const warnings = findings.filter((f) => f.severity === 'warning').length;
  const infos = findings.filter((f) => f.severity === 'info').length;
  const unknownFindings = findings.filter((f) => f.kind === 'unknown-type').length;
  const verdict =
    errors > 0 ? 'incompatible' : unknownFindings > 0 || warnings > 0 ? 'unknown' : 'compatible';
  return {
    verdict,
    summary: {
      totalFindings: findings.length,
      errors,
      warnings,
      infos,
      unknownFindings,
    },
    findings,
  };
}

function evidenceOf(e: RegionEntry, types: Map<string, TypeModel>): Record<string, unknown> {
  const m = resolveType(types, e.typeId);
  return {
    label: e.label,
    slot: e.raw.slot,
    offset: e.raw.offset,
    type: e.typeId,
    typeLabel: m.label,
    bytes: m.raw.numberOfBytes,
    ...(e.contract ? { contract: e.contract } : {}),
  };
}

function insertedKind(role: Ctx['role']): FindingKind {
  if (role === 'value') return 'mapping-value-changed';
  return role === 'root' ? 'field-inserted' : 'struct-member-inserted';
}

function movedKind(role: Ctx['role']): FindingKind {
  return role === 'root' ? 'field-moved' : 'struct-member-moved';
}

function compareRegion(
  oldEntries: RegionEntry[],
  newEntries: RegionEntry[],
  path: string[],
  ctx: Ctx,
  pair: LayoutPair,
  fb: FindingBuilder,
  gapRe: RegExp,
): void {
  const oldMap = new Map(oldEntries.map((e) => [e.label, e]));
  const newMap = new Map(newEntries.map((e) => [e.label, e]));
  const oldEnd = oldEntries.reduce((acc, e) => (e.end > acc ? e.end : acc), 0n);

  const freed: Interval[] = [];
  const /** Pass B 需要当作“新字段”处理的 label（gap 被同名字段替换等情况） */
    forceNewLabels = new Set<string>();
  let gapShrank = false; // 有同名 gap 被缩小（区分“正常追加”与“消耗储备”）

  /* ---- Pass A：旧区域中的每个成员 ---- */
  for (const oe of oldEntries) {
    const ne = newMap.get(oe.label);
    if (!ne) {
      const kind: FindingKind =
        ctx.role === 'root' ? 'field-removed' : 'struct-member-removed';
      if (oe.isGap) {
        // gap 被整体删除：平坦兼容（其区间可能正好承接新字段），
        // 但安全标记消失，人工确认 -> warning
        fb.add(
          'gap-removed',
          'warning',
          [...path, oe.label],
          `gap 储备 ${[...path, oe.label].join('.')} 被整体删除；需人工确认其区间被新字段精确承接`,
          evidenceOf(oe, pair.old.types),
        );
        freed.push({ start: oe.start, end: oe.end });
        gapShrank = true;
        continue;
      }
      fb.add(
        kind,
        'error',
        [...path, oe.label],
        `字段被删除: ${[...path, oe.label].join('.')}（删除已有存储字段会改变后续布局语义）`,
        evidenceOf(oe, pair.old.types),
      );
      continue;
    }

    if (oe.isGap || ne.isGap) {
      if (oe.isGap && !ne.isGap) {
        // gap 数组被普通字段替换：释放整块区间，但保留警告（安全标记消失）
        fb.add(
          'gap-removed',
          'warning',
          [...path, oe.label],
          `gap 储备数组 ${[...path, oe.label].join('.')} 被移除；其释放区间需由新字段精确承接`,
          evidenceOf(oe, pair.old.types),
          evidenceOf(ne, pair.new.types),
        );
        freed.push({ start: oe.start, end: oe.end });
        gapShrank = true;
        forceNewLabels.add(ne.label);
        // 替换它的字段按新字段处理（Pass B 中 forceNewLabels 会捕获）
        continue;
      }
      if (!oe.isGap && ne.isGap) {
        fb.add(
          insertedKind(ctx.role),
          'error',
          [...path, ne.label],
          `已有字段 ${[...path, ne.label].join('.')} 被替换为 gap 数组，类型语义变化`,
          evidenceOf(oe, pair.old.types),
          evidenceOf(ne, pair.new.types),
        );
        continue;
      }
      // 两边都是 gap：仅允许原地缩小。
      // 0 长 gap（数组缩到空）不占存储，其起点是新布局的声明游标，
      // 必然落在旧区间之后；这不构成移动，视为完全清空。
      if (ne.end === ne.start) {
        const freedBytes = oe.end - oe.start;
        gapShrank = true;
        fb.add(
          'gap-reduced',
          'info',
          [...path, oe.label],
          `gap ${[...path, oe.label].join('.')} 完全清空（长度缩为 0），释放 ${freedBytes} 字节储备`,
          {
            ...evidenceOf(oe, pair.old.types),
            spanBytes: String(oe.end - oe.start),
          },
          {
            ...evidenceOf(ne, pair.new.types),
            spanBytes: '0',
          },
        );
        freed.push({ start: oe.start, end: oe.end });
      } else if (ne.start < oe.start || ne.end > oe.end) {
        fb.add(
          'gap-moved',
          'error',
          [...path, oe.label],
          `gap ${[...path, oe.label].join('.')} 发生移动或扩张，仅允许原地缩小`,
          {
            ...evidenceOf(oe, pair.old.types),
            spanBytes: String(oe.end - oe.start),
          },
          {
            ...evidenceOf(ne, pair.new.types),
            spanBytes: String(ne.end - ne.start),
          },
        );
      } else if (ne.start > oe.start || ne.end < oe.end) {
        const freedBytes =
          (ne.start > oe.start ? ne.start - oe.start : 0n) +
          (oe.end > ne.end ? oe.end - ne.end : 0n);
        gapShrank = true;
        fb.add(
          'gap-reduced',
          'info',
          [...path, oe.label],
          `gap ${[...path, oe.label].join('.')} 原地缩小，释放 ${freedBytes} 字节储备`,
          {
            ...evidenceOf(oe, pair.old.types),
            spanBytes: String(oe.end - oe.start),
          },
          {
            ...evidenceOf(ne, pair.new.types),
            spanBytes: String(ne.end - ne.start),
          },
        );
        if (ne.start > oe.start) freed.push({ start: oe.start, end: ne.start });
        if (ne.end < oe.end) freed.push({ start: ne.end, end: oe.end });
      }
      continue;
    }

    // 普通保留字段：起点必须不变
    if (ne.start !== oe.start) {
      fb.add(
        movedKind(ctx.role),
        'error',
        [...path, oe.label],
        `字段 ${[...path, oe.label].join('.')} 位置移动: 字节偏移 ${oe.start} -> ${ne.start}`,
        evidenceOf(oe, pair.old.types),
        evidenceOf(ne, pair.new.types),
      );
      // 位置已变，类型再比较意义不大，跳过以免噪音
      continue;
    }

    // 末端成员判定：旧区域中结束位置最靠后的成员
    const isTailMember = oe.end >= oldEnd && ctx.tail;
    compareTypes(
      resolveType(pair.old.types, oe.typeId),
      resolveType(pair.new.types, ne.typeId),
      [...path, oe.label],
      { relocatable: ctx.relocatable, tail: isTailMember, role: 'member' },
      pair,
      fb,
      gapRe,
    );
  }

  /* ---- Pass B：新区域中多出的成员（含 gap-removed 后承接的新字段）---- */
  const freedUnion = unionSorted(freed);
  /** 结构体内部：只有位于父区域末端或处于 keccak 寻址区时，尾部追加才安全 */
  const appendAllowed = ctx.tail || ctx.relocatable;
  for (const ne of newEntries) {
    if (oldMap.has(ne.label) && !forceNewLabels.has(ne.label)) {
      continue;
    }
    const span: Interval = { start: ne.start, end: ne.end };
    const inAppendZone = ne.start >= oldEnd;
    const notCovered = uncovered(span, freedUnion);
    const fullyInFreed = notCovered.length === 0 && ne.end > ne.start;

    if (ne.isGap) {
      if (inAppendZone && appendAllowed) {
        fb.add(
          'gap-extended-trailing',
          'info',
          [...path, ne.label],
          `尾部新增 gap 储备 ${[...path, ne.label].join('.')}，不影响已有字段`,
          undefined,
          evidenceOf(ne, pair.new.types),
        );
      } else if (notCovered.length === 0) {
        fb.add(
          'gap-reduced',
          'info',
          [...path, ne.label],
          `gap 在旧储备区间内重新排布: ${[...path, ne.label].join('.')}`,
          undefined,
          evidenceOf(ne, pair.new.types),
        );
      } else {
        fb.add(
          insertedKind(ctx.role),
          'error',
          [...path, ne.label],
          `新 gap ${[...path, ne.label].join('.')} 出现在非储备区域，会挤占已有存储`,
          undefined,
          evidenceOf(ne, pair.new.types),
        );
      }
      continue;
    }

    if (fullyInFreed) {
      fb.add(
        'appended',
        'info',
        [...path, ne.label],
        `新字段 ${[...path, ne.label].join('.')} 由 gap 释放区间承接（字节 ${ne.start}..${ne.end}）`,
        undefined,
        evidenceOf(ne, pair.new.types),
      );
    } else if (notCovered.length === 0 && ne.end === ne.start) {
      // 零长度字段落在储备内（极端情况）
      fb.add('appended', 'info', [...path, ne.label], `零长度新字段 ${ne.label}`, undefined,
        evidenceOf(ne, pair.new.types));
    } else if (inAppendZone && appendAllowed && notCovered.every((p) => p.start >= oldEnd)) {
      fb.add(
        'appended',
        'info',
        [...path, ne.label],
        `尾部追加字段 ${[...path, ne.label].join('.')}（新存储位于旧布局末端之后）`,
        undefined,
        evidenceOf(ne, pair.new.types),
      );
    } else {
      fb.add(
        insertedKind(ctx.role),
        'error',
        [...path, ne.label],
        `新字段 ${[...path, ne.label].join('.')} 插入到已有存储区中间（字节 ${ne.start} 不在 gap 释放区间/末端追加区内）`,
        { regionEndBytes: String(oldEnd) },
        evidenceOf(ne, pair.new.types),
      );
    }

    // 追加字段自身的类型仍必须是检查器认识的类型
    const newModel = resolveType(pair.new.types, ne.typeId);
    if (newModel.kind === 'unknown') {
      fb.add(
        'unknown-type',
        'warning',
        [...path, ne.label],
        `新字段 ${[...path, ne.label].join('.')} 引用了无法判定的类型 ${ne.typeId}（${
          newModel.unknownReason ?? '未知原因'
        }），保守视为无法确认兼容性`,
        undefined,
        { ...evidenceOf(ne, pair.new.types), encoding: newModel.encoding },
      );
    } else {
      // 递归校验新类型定义内部不存在未知类型
      assertTypeKnown(newModel, pair.new.types, [...path, ne.label], fb, new Set());
    }
  }

  /* ---- gap 边界：缩小储备后需求溢出到旧区域末端之外 ---- */
  if (gapShrank) {
    const newNonGapEnd = newEntries
      .filter((e) => !e.isGap && !oldMap.has(e.label))
      .reduce((acc, e) => (e.end > acc ? e.end : acc), 0n);
    if (newNonGapEnd > oldEnd) {
      fb.add(
        'gap-overflow',
        'warning',
        path.length ? path : ['<root>'],
        `gap 储备已耗尽，新增字段越过旧区域末端（旧末端字节 ${oldEnd}，新字段末端字节 ${newNonGapEnd}）；若存在后继继承合约将发生碰撞`,
        { regionEndBytes: String(oldEnd) },
        { newFieldsEndBytes: String(newNonGapEnd) },
      );
    }
  }
}

/* --------------------------- 继承线性化分析 ---------------------------- */

function analyzeInheritance(
  oldEntries: RegionEntry[],
  newEntries: RegionEntry[],
  pair: LayoutPair,
  fb: FindingBuilder,
): void {
  const oldGroups = contractGroups(oldEntries);
  const newGroups = contractGroups(newEntries);
  if (oldGroups.length === 0 || newGroups.length === 0) return; // 缺少 contract 字段（旧编译器）

  const oldNames = oldGroups.map((g) => g.name);
  const newNames = newGroups.map((g) => g.name);
  const oldSet = new Set(oldNames);
  const newSet = new Set(newNames);

  // 集合相同但顺序变化 => 线性化重排
  const inserted = newNames.filter((n) => !oldSet.has(n));
  const removed = oldNames.filter((n) => !newSet.has(n));
  if (inserted.length === 0 && removed.length === 0) {
    if (oldNames.join('|') !== newNames.join('|')) {
      fb.add(
        'inheritance-reordered',
        'error',
        ['<inheritance>'],
        `继承线性化顺序变化: ${oldNames.join(' -> ')}  =>  ${newNames.join(' -> ')}`,
        { order: oldNames },
        { order: newNames },
      );
    }
    return;
  }

  // 新基类插入：若其位置位于任一保留基类之前，则该基类变量整体被挤位
  for (const name of inserted) {
    const g = newGroups.find((x) => x.name === name)!;
    const laterKept = newNames
      .slice(newNames.indexOf(name) + 1)
      .filter((n) => oldSet.has(n));
    if (laterKept.length > 0) {
      // 若被挤位的变量没有被任何 gap 释放覆盖（平坦区域比较已产生 moved 错误），
      // 这里补充根因（继承插入）。
      fb.add(
        'inheritance-inserted',
        'error',
        ['<inheritance>', name],
        `新基类 ${name} 插入到 ${laterKept.join(', ')} 之前，后者的存储变量被整体推移 ${g.spanBytes} 字节`,
        undefined,
        {
          insertedContract: name,
          insertedVars: g.entries.map((e) => e.label),
          shiftsContracts: laterKept,
          insertedSpanBytes: g.spanBytes,
        },
      );
    }
  }
}

interface ContractGroup {
  name: string;
  entries: RegionEntry[];
  spanBytes: string;
}

function contractGroups(entries: RegionEntry[]): ContractGroup[] {
  const groups: ContractGroup[] = [];
  let cur: ContractGroup | null = null;
  for (const e of entries) {
    if (!e.contract) return []; // 任一条目缺 contract 字段则放弃该分析
    if (!cur || cur.name !== e.contract) {
      cur = { name: e.contract, entries: [], spanBytes: '0' };
      groups.push(cur);
    }
    cur.entries.push(e);
  }
  for (const g of groups) {
    const start = g.entries[0]!.start;
    const end = g.entries.reduce((acc, e) => (e.end > acc ? e.end : acc), start);
    g.spanBytes = String(end - start);
  }
  return groups;
}

/* ------------------------------ 类型递归 ------------------------------- */

/** 检查新类型定义内部（struct 成员、数组元素、mapping kv）是否引用未知类型 */
function assertTypeKnown(
  m: TypeModel,
  types: Map<string, TypeModel>,
  path: string[],
  fb: FindingBuilder,
  seen: Set<string>,
): void {
  if (seen.has(m.typeId)) return;
  seen.add(m.typeId);
  const checkRef = (id: string | undefined, suffix: string): void => {
    if (!id) return;
    const ref = resolveType(types, id);
    if (ref.kind === 'unknown') {
      fb.add(
        'unknown-type',
        'warning',
        [...path, suffix],
        `引用了无法判定的类型 ${id}（${ref.unknownReason ?? '未知原因'}），保守视为无法确认兼容性`,
        undefined,
        { type: id, label: ref.label, encoding: ref.encoding },
      );
    } else {
      assertTypeKnown(ref, types, [...path, suffix], fb, seen);
    }
  };
  if (m.kind === 'struct') {
    for (const mem of m.members ?? []) checkRef(mem.typeId, mem.label);
  } else if (m.kind === 'fixed-array' || m.kind === 'dynamic-array') {
    checkRef(m.baseTypeId, '[*]');
  } else if (m.kind === 'mapping') {
    checkRef(m.keyTypeId, '[key]');
    checkRef(m.valueTypeId, '[value]');
  }
}

function elementaryClass(typeId: string, kind: TypeModel['kind']): string {
  if (kind === 'enum') return 'enum';
  if (kind === 'contract') return 'contract';
  if (/^t_address/.test(typeId)) return 'address';
  if (/^t_bool/.test(typeId)) return 'bool';
  if (/^t_uint/.test(typeId)) return 'uint';
  if (/^t_int/.test(typeId)) return 'int';
  if (/^t_bytes\d+/.test(typeId)) return 'bytesN';
  if (/^t_function/.test(typeId)) return 'function';
  return 'unknown';
}

function compareTypes(
  ot: TypeModel,
  nt: TypeModel,
  path: string[],
  ctx: Ctx,
  pair: LayoutPair,
  fb: FindingBuilder,
  gapRe: RegExp,
): void {
  if (ot.kind === 'unknown' || nt.kind === 'unknown') {
    fb.add(
      'unknown-type',
      'warning',
      path,
      `无法判定的类型: ${ot.kind === 'unknown' ? ot.typeId : nt.typeId}（${
        ot.kind === 'unknown' ? ot.unknownReason : nt.unknownReason
      }），保守视为无法确认兼容性`,
      { type: ot.typeId, label: ot.label, encoding: ot.encoding },
      { type: nt.typeId, label: nt.label, encoding: nt.encoding },
    );
    return;
  }

  if (ot.kind !== nt.kind) {
    fb.add(
      remapByRole('type-changed', ctx.role),
      'error',
      path,
      `类型种类变化: ${ot.kind}(${ot.label}) -> ${nt.kind}(${nt.label})`,
      { type: ot.typeId, kind: ot.kind, label: ot.label },
      { type: nt.typeId, kind: nt.kind, label: nt.label },
    );
    return;
  }

  switch (ot.kind) {
    case 'elementary': {
      const oc = elementaryClass(ot.typeId, ot.kind);
      const nc = elementaryClass(nt.typeId, nt.kind);
      if (oc === 'unknown' || nc === 'unknown') {
        fb.add('unknown-type', 'warning', path, `未识别的基本类型标识符`,
          { type: ot.typeId }, { type: nt.typeId });
        return;
      }
      if (oc !== nc) {
        fb.add(remapByRole('type-changed', ctx.role), 'error', path,
          `基本类型种类变化: ${oc} -> ${nc}`,
          { type: ot.typeId, label: ot.label }, { type: nt.typeId, label: nt.label });
        return;
      }
      if (ot.numberOfBytes !== nt.numberOfBytes) {
        fb.add(remapByRole('width-changed', ctx.role), 'error', path,
          `位宽变化: ${ot.label}(${ot.numberOfBytes}B) -> ${nt.label}(${nt.numberOfBytes}B)`,
          { type: ot.typeId, bytes: ot.raw.numberOfBytes },
          { type: nt.typeId, bytes: nt.raw.numberOfBytes });
        return;
      }
      if (ot.label !== nt.label) {
        fb.add('renamed', 'info', path, `类型别名变化但布局一致: ${ot.label} -> ${nt.label}`,
          { type: ot.typeId }, { type: nt.typeId });
      }
      return;
    }

    case 'enum': {
      if (ot.numberOfBytes !== nt.numberOfBytes) {
        fb.add('enum-width-changed', 'error', path,
          `enum 底层宽度变化: ${ot.numberOfBytes}B -> ${nt.numberOfBytes}B`,
          { label: ot.label, bytes: ot.raw.numberOfBytes },
          { label: nt.label, bytes: nt.raw.numberOfBytes });
      } else if (ot.label !== nt.label) {
        fb.add('renamed', 'info', path, `enum 重命名但宽度一致: ${ot.label} -> ${nt.label}`,
          { type: ot.typeId }, { type: nt.typeId });
      }
      return;
    }

    case 'contract': {
      if (ot.label !== nt.label) {
        fb.add('renamed', 'info', path, `合约类型变化（均为 20 字节 address）: ${ot.label} -> ${nt.label}`,
          { type: ot.typeId }, { type: nt.typeId });
      }
      return;
    }

    case 'bytes':
    case 'string':
      return; // 同类即兼容（bytes/string 互变已被 kind 不同拦截：均映射为不同 kind）

    case 'fixed-array': {
      if (ot.length !== nt.length) {
        fb.add(remapByRole('array-length-changed', ctx.role), 'error', path,
          `定长数组长度变化: ${ot.length} -> ${nt.length}`,
          { type: ot.typeId, length: String(ot.length), bytes: ot.raw.numberOfBytes },
          { type: nt.typeId, length: String(nt.length), bytes: nt.raw.numberOfBytes });
        return;
      }
      compareTypes(
        resolveType(pair.old.types, ot.baseTypeId!),
        resolveType(pair.new.types, nt.baseTypeId!),
        [...path, '[*]'],
        // 数组元素重复排布：元素增长会推移后续元素，tail=false
        { relocatable: ctx.relocatable, tail: false, role: 'base' },
        pair, fb, gapRe,
      );
      return;
    }

    case 'dynamic-array': {
      compareTypes(
        resolveType(pair.old.types, ot.baseTypeId!),
        resolveType(pair.new.types, nt.baseTypeId!),
        [...path, '[]'],
        // 变长数组元素位于 keccak(slot) 寻址区
        { relocatable: true, tail: true, role: 'base' },
        pair, fb, gapRe,
      );
      return;
    }

    case 'mapping': {
      const ok = resolveType(pair.old.types, ot.keyTypeId!);
      const nk = resolveType(pair.new.types, nt.keyTypeId!);
      const keyPath = [...path, '[key]'];
      if (ok.kind === 'unknown' || nk.kind === 'unknown') {
        fb.add('unknown-type', 'warning', keyPath,
          'mapping 键类型无法判定，保守视为无法确认兼容性',
          { type: ok.typeId }, { type: nk.typeId });
      } else if (ok.kind !== nk.kind || ok.numberOfBytes !== nk.numberOfBytes ||
          elementaryClass(ok.typeId, ok.kind) !== elementaryClass(nk.typeId, nk.kind)) {
        fb.add('mapping-key-changed', 'error', keyPath,
          `mapping 键类型变化: ${ok.label} -> ${nk.label}`,
          { type: ok.typeId, label: ok.label }, { type: nk.typeId, label: nk.label });
      }
      compareTypes(
        resolveType(pair.old.types, ot.valueTypeId!),
        resolveType(pair.new.types, nt.valueTypeId!),
        [...path, '[value]'],
        // mapping 值位于 keccak(key,slot) 独立寻址区
        { relocatable: true, tail: true, role: 'value' },
        pair, fb, gapRe,
      );
      return;
    }

    case 'struct': {
      if (ot.label !== nt.label) {
        fb.add('renamed', 'info', path, `结构体重命名: ${ot.label} -> ${nt.label}`,
          { type: ot.typeId }, { type: nt.typeId });
      }
      const oe = buildStructEntries(ot, pair.old.types, gapRe);
      const ne = buildStructEntries(nt, pair.new.types, gapRe);
      compareRegion(oe, ne, path, ctx, pair, fb, gapRe);
      return;
    }
  }
}

function remapByRole(kind: FindingKind, role: Ctx['role']): FindingKind {
  if (role === 'key') return 'mapping-key-changed';
  if (role === 'value') {
    if (kind === 'type-changed' || kind === 'width-changed') return 'mapping-value-changed';
  }
  if (role === 'base') {
    if (kind === 'type-changed' || kind === 'width-changed') return 'array-base-changed';
    if (kind === 'array-length-changed') return kind;
  }
  return kind;
}
