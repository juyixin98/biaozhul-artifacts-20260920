/**
 * 跨模块数据契约。
 *
 * 分层：
 *   schema   —— 类型化模型（对象类型、字段、列表基数上界、片段声明）
 *   query    —— 解析后的查询 AST（选择字段、别名、片段展开、变量）
 *   cost     —— 逐字段成本树（估计与实际共用同一结构）
 *   result   —— 执行结果（完整 / 部分结果 + 取消标注）
 */

// ---------------------------------------------------------------------------
// Schema 契约
// ---------------------------------------------------------------------------

export type PrimitiveKind = 'STRING' | 'INT' | 'FLOAT' | 'BOOL' | 'ID';

export interface FieldDef {
  /** 字段名（对象类型内唯一） */
  name: string;
  /** 标量字段为基础类型；对象/列表字段为对象类型名 */
  type: string;
  /** 是否列表（仅对象类型字段允许为列表） */
  list: boolean;
  /**
   * 列表基数的【声明上界】。未知列表规模一律使用该上界，
   * 禁止在静态估计阶段探测真实行数。
   */
  declaredUpperBound: number;
  /** 字段倍率：解析一个该字段的成本权重 */
  multiplier: number;
}

export interface ObjectTypeDef {
  name: string;
  fields: FieldDef[];
}

export interface FragmentDef {
  name: string;
  /** 片段适用的对象类型 */
  onType: string;
  fields: SelectionDef[];
}

export interface SchemaDocument {
  queryType: string;
  types: Record<string, ObjectTypeDef>;
  fragments: Record<string, FragmentDef>;
}

// ---------------------------------------------------------------------------
// 查询 AST 契约
// ---------------------------------------------------------------------------

export interface FieldSelection {
  kind: 'field';
  /** 查询文本里写的名字 */
  name: string;
  /** 响应键别名（未起别名时等于 name） */
  alias: string;
  args: Record<string, unknown>;
  selections: SelectionDef[];
}

export interface FragmentSpread {
  kind: 'fragmentSpread';
  name: string;
}

export type SelectionDef = FieldSelection | FragmentSpread;

export interface VariableDef {
  name: string;
  declaredType: string;
  defaultValue?: unknown;
}

export interface ParsedQuery {
  source: string;
  variables: VariableDef[];
  selections: SelectionDef[];
}

export interface VariableValues {
  [name: string]: unknown;
}

// ---------------------------------------------------------------------------
// 成本树契约
// ---------------------------------------------------------------------------

export interface CostNode {
  /** 响应键路径，如 "users.0.posts.2.title"；根为 "$" */
  path: string;
  /** 字段名（片段展开节点为片段名） */
  field: string;
  /** 该节点自身成本（字段倍率；列表展开按元素逐行计入） */
  selfCost: number;
  /** 子节点成本合计（片段展开、嵌套字段、列表元素） */
  childCost: number;
  /** self + child */
  total: number;
  /** 列表上界 / 实际元素数（仅列表节点存在） */
  cardinality?: { declared: number; actual?: number };
  /** 该节点经由哪个片段展开而来（重复片段各自独立计费） */
  viaFragment?: string;
  /** PARTIAL 时：该节点解析中途被取消（有已扣减成本但无对应数据） */
  incomplete?: boolean;
  children: CostNode[];
}

// ---------------------------------------------------------------------------
// 执行结果契约
// ---------------------------------------------------------------------------

export type FailureCategory =
  | 'INPUT_INVALID' // 适用的输入错误：语法/契约不合法
  | 'STATE_CONFLICT' // 状态冲突：别名重复、变量重复、片段循环等
  | 'RESOURCE_EXHAUSTED' // 资源耗尽：静态预算不足 / 运行时预算超限
  | 'COMPUTATION_FAILED'; // 计算失败：夹具/适配层缺表缺字段等

export interface CancellationInfo {
  /** 触发取消时的响应键路径 */
  path: string;
  /** 取消发生在哪个片段内（如有） */
  fragment?: string;
  /** 取消瞬间已扣减成本 */
  spent: number;
  budget: number;
  reason: string;
}

export interface ExecutionResult {
  runId: string;
  status: 'COMPLETE' | 'PARTIAL';
  data: Record<string, unknown>;
  /** 逐字段实际成本树（PARTIAL 时只含已完成部分） */
  costTree: CostNode;
  estimatedCost: number;
  actualCost: number;
  budget: number;
  /** 估计是否落在实际夹具成本之下（用于验证场景） */
  estimateBelowActual: boolean;
  cancelled?: CancellationInfo;
}
