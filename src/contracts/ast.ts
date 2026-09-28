/**
 * 类型化查询的 AST 定义。
 *
 * 查询是一个纯数据对象（不是查询字符串），因此“字符数”从结构上就不可能
 * 参与成本计算。执行内核只消费 AST，从不读取任何源码文本。
 */

export type VarValue = string | number | boolean;

/** 变量声明：运行前必须先通过类型校验。 */
export interface VarDecl {
  name: string;
  type: 'string' | 'int' | 'bool';
  value: VarValue;
}

/** 叶子字段：取自当前对象的标量字段。 */
export interface ScalarFieldNode {
  kind: 'field';
  /** 结果中的别名；同一层级重复别名是输入错误。 */
  alias: string;
  /** 类型上的字段名。 */
  field: string;
}

/** 列表字段：沿关系展开，是基数乘法发生的地方。 */
export interface ListFieldNode {
  kind: 'list';
  alias: string;
  /** 关系名，必须在当前类型的 relations 中声明。 */
  relation: string;
  /** 未知列表规模采用的“声明上界”；必填，禁止留空猜测。 */
  declaredUpperBound: number;
  /** 列表上的过滤变量（如 { var: 'orgId', op: 'eq' }），先做类型校验。 */
  args?: Array<{ var: string; op: 'eq' }>;
  /** 子选择，必须非空（空选择本身无意义且是输入错误）。 */
  children: FieldNode[];
}

/** 片段展开：include 一个具名片段，重复 include 必须重复计费。 */
export interface FragmentSpreadNode {
  kind: 'spread';
  /** 片段名；解析时按出现次数展开（不记忆化、不去重）。无别名。 */
  fragment: string;
}

export type FieldNode = ScalarFieldNode | ListFieldNode | FragmentSpreadNode;

/**
 * 片段：具名的字段集合。
 *
 * - inline=true 的片段只允许被内联展开（测试夹具中的循环片段用它来
 *   构造 A -> B -> A 的静态循环）；
 * - 普通片段可被任意 include；
 * - 片段在 schema 中静态声明，禁止从查询输入动态定义片段，从而避免
 *   “由查询自身生成答案”。
 */
export interface FragmentDef {
  name: string;
  fields: FieldNode[];
  inline?: boolean;
}

export interface Query {
  /** 查询的根类型名。 */
  root: string;
  /** 根对象 id（变量之外的固定定位参数）。 */
  rootId: number;
  fields: FieldNode[];
  vars?: VarDecl[];
  /** include 的具名片段名（按出现顺序，重复出现重复计费）。 */
  fragments?: string[];
}
