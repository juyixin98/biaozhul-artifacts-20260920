/**
 * 类型契约（schema）。
 *
 * 这是“类型化查询”的类型来源：每个对象类型声明标量字段（含倍率）与
 * 关系（含目标类型）。静态成本估计与变量类型校验都只依赖该声明。
 */

export type ScalarTypeName = 'string' | 'int' | 'bool';

export interface ScalarFieldDef {
  type: ScalarTypeName;
  /**
   * 字段成本倍率。读取一个重字段（如富文本）可以比标量贵，
   * 叶子成本 = 1 * multiplier。必须为正整数。
   */
  multiplier: number;
}

export interface RelationDef {
  to: string;
}

export interface ObjectTypeDef {
  fields: Record<string, ScalarFieldDef>;
  relations: Record<string, RelationDef>;
}

export interface Schema {
  types: Record<string, ObjectTypeDef>;
  fragments: Record<string, import('./ast.js').FragmentDef>;
}
