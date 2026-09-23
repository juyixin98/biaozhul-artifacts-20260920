/**
 * 输入类型：Solidity 编译器（solc）`storage-layout` JSON 输出的最小形状。
 *
 * 典型来源：solc 标准 JSON 输出的
 *   contracts[sourceName][contractName].storageLayout = { storage, types, ... }
 *
 * 本检查器只依赖编译器文档中稳定的字段：
 *   storage[].label / astId / contract / offset / slot / type
 *   types[id].label / encoding / numberOfBytes / members / key / value / base / length
 */

export type TypeEncoding =
  | 'inplace'
  | 'mapping'
  | 'dynamic_array'
  | 'bytes'
  | 'packed_array'
  | string;

export interface StorageLayoutTypeMember {
  label: string;
  offset: number;
  slot: string;
  type: string;
}

export interface StorageLayoutType {
  label: string;
  encoding: TypeEncoding | string;
  numberOfBytes: string;
  /** inplace 结构体才有 */
  members?: StorageLayoutTypeMember[];
  /** mapping 才有 */
  key?: string;
  /** mapping / dynamic_array 才有（mapping 的 value 是值类型 id） */
  value?: string;
  /** packed_array 才有 */
  base?: string;
  /** fixed-size array: "dynarray" 编码为 dynamic_array；t_array(N) 的 numberOfBytes 为整数字节数 */
  length?: string;
  /** 旧版编译器可能出现的字段，保留未知属性 */
  [extra: string]: unknown;
}

export interface StorageLayoutItem {
  label: string;
  offset: number;
  slot: string;
  type: string;
  /** 0.8.x 新编译器提供；0.5~0.7 不提供，此时 contract 为 undefined */
  contract?: string;
  /** 0.8.x 提供 */
  src?: string;
  astId?: number;
  [extra: string]: unknown;
}

export interface SolcStorageLayout {
  storage: StorageLayoutItem[];
  types: Record<string, StorageLayoutType>;
  /** 0.8 新 IR 布局可能带这两个字段，忽略即可 */
  enums?: Record<string, unknown>;
  namespaces?: Record<string, unknown>;
}

/** 与 solc 字段一致的顶层容器（方便直接粘贴 artifact 片段） */
export interface SolcArtifactLike {
  storageLayout?: SolcStorageLayout;
}

/* -------------------------------------------------------------------------- */
/* 检查结果类型                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * - compatible：没有任何问题（可能带有 info 提示）
 * - unknown：存在检查器无法判定的未知类型，保守起见不得视为兼容
 * - incompatible：存在明确破坏存储兼容性的变化
 */
export type Verdict = 'compatible' | 'unknown' | 'incompatible';

export type Severity = 'error' | 'warning' | 'info';

/**
 * 问题分类。命名即规则：
 *  field-removed          根变量被删除
 *  field-moved            同合约/无继承信息下，变量 slot/offset 前移或整体移位
 *  field-inserted         新变量插入到已有存储区中间（含打包 slot 内部插入）
 *  width-changed          同位置基本类型宽度/种类变化
 *  type-changed           类型种类变化（mapping 与非 mapping 等）
 *  array-length-changed   定长数组长度变化
 *  array-base-changed     数组元素类型变化
 *  mapping-key-changed    mapping 键类型变化
 *  mapping-value-changed  mapping 值类型变化
 *  struct-member-removed  结构体成员被删除
 *  struct-member-moved    结构体成员偏移变化
 *  struct-member-inserted 结构体在非允许追加的上下文中插入/增长
 *  enum-width-changed     enum 底层宽度变化
 *  gap-overflow           新变量超出 __gap 可用空间
 *  gap-moved              gap 区间发生非规则移动
 *  gap-removed            __gap 被整体删除
 *  inheritance-inserted   新的中间基类把后续变量整体挤位
 *  inheritance-reordered  线性化合约顺序变化
 *  unknown-type           出现检查器不认识的类型编码/定义
 *  gap-reduced            info：可用 gap 被正常消耗（允许的变化）
 *  gap-extended-trailing  info：尾部 gap 扩大
 *  appended               info：符合规则的尾部追加
 *  renamed                info：仅类型名变化，结构一致
 */
export type FindingKind =
  | 'field-removed'
  | 'field-moved'
  | 'field-inserted'
  | 'width-changed'
  | 'type-changed'
  | 'array-length-changed'
  | 'array-base-changed'
  | 'mapping-key-changed'
  | 'mapping-value-changed'
  | 'struct-member-removed'
  | 'struct-member-moved'
  | 'struct-member-inserted'
  | 'enum-width-changed'
  | 'gap-overflow'
  | 'gap-moved'
  | 'gap-removed'
  | 'inheritance-inserted'
  | 'inheritance-reordered'
  | 'unknown-type'
  | 'gap-reduced'
  | 'gap-extended-trailing'
  | 'appended'
  | 'renamed';

export interface Evidence {
  [key: string]: unknown;
}

export interface Finding {
  kind: FindingKind;
  severity: Severity;
  /** 最短差异路径，例如 ["b", "x", "[*]", "y"] */
  path: string[];
  /** 人类可读说明 */
  message: string;
  /** 编译器输出中的原始证据（旧/新 slot、offset、类型 id 等） */
  oldEvidence?: Evidence;
  newEvidence?: Evidence;
}

export interface CheckOptions {
  /**
   * 识别 gap 数组变量名（按 label 匹配）。
   * 默认精确匹配 __gap（OpenZeppelin 约定）。
   */
  gapNamePattern?: RegExp;
}

export interface CheckSummary {
  totalFindings: number;
  errors: number;
  warnings: number;
  infos: number;
  unknownFindings: number;
}

export interface CompatReport {
  verdict: Verdict;
  summary: CheckSummary;
  /** 按最短差异路径排序后的全部发现 */
  findings: Finding[];
}
