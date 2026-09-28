/**
 * 静态成本估计器。
 *
 * 对“片段展开之后”的选择集计成本，输入只有 AST 结构与 schema 声明，
 * 因此查询字符数在任何路径上都不可达，不可能计入成本。
 *
 * 计成本规则：
 * - field 叶子：unitContribution = multiplier × （从根到该字段经过的
 *   所有列表 declaredUpperBound 之积）；
 * - list 节点自身 self=0，但它改变子树的基数倍乘；
 * - spread 不允许残留（展开后已消除）；
 * - estimatedTotal = 所有叶子 unitContribution 之和；重复展开得到的
 *   重复字段是重复的叶子，因此重复片段/别名在去重前就各自计费。
 */
import type { FieldNode } from '../contracts/ast.js';
import type { Schema } from '../contracts/schema.js';
import type { CostNode } from '../contracts/cost.js';
import { fail } from '../contracts/errors.js';

export interface StaticEstimate {
  tree: CostNode;
  total: number;
}

function estimateFields(
  fields: FieldNode[],
  schema: Schema,
  typeName: string,
  cardinalityProduct: number,
  path: string[],
): CostNode[] {
  const typeDef = schema.types[typeName]!;
  return fields.map((node): CostNode => {
    if (node.kind === 'spread') {
      fail('INPUT_ERROR', 'MALFORMED_QUERY', 'spread reached estimator without expansion', path);
    }
    if (node.kind === 'field') {
      const multiplier = typeDef.fields[node.field]!.multiplier;
      return {
        label: `field:${node.alias}`,
        kind: 'field',
        self: multiplier,
        unitContribution: multiplier * cardinalityProduct,
        children: [],
        runtimeStatus: 'pending',
      };
    }
    const rel = typeDef.relations[node.relation]!;
    const bound = node.declaredUpperBound;
    const children = estimateFields(
      node.children,
      schema,
      rel.to,
      cardinalityProduct * bound,
      [...path, node.alias],
    );
    return {
      label: `list:${node.alias}`,
      kind: 'list',
      self: 0,
      // 列表节点自身无叶子成本；其子代已携带 ×bound 的单位贡献。
      unitContribution: 0,
      cardinality: bound,
      children,
      runtimeStatus: 'pending',
    };
  });
}

export function sumLeaves(nodes: CostNode[]): number {
  let sum = 0;
  for (const n of nodes) {
    sum += n.unitContribution;
    sum += sumLeaves(n.children);
  }
  return sum;
}

export function estimateStatic(
  rootType: string,
  expandedFields: FieldNode[],
  schema: Schema,
): StaticEstimate {
  if (!schema.types[rootType]) {
    fail('INPUT_ERROR', 'UNKNOWN_TYPE', `unknown root type: ${rootType}`);
  }
  const children = estimateFields(expandedFields, schema, rootType, 1, []);
  const total = sumLeaves(children);
  const tree: CostNode = {
    label: `root:${rootType}`,
    kind: 'root',
    self: 0,
    unitContribution: 0,
    children,
    runtimeStatus: 'pending',
  };
  return { tree, total };
}
