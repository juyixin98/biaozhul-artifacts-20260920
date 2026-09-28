/**
 * 静态成本估计器。
 *
 * 计费规则（与运行时执行器严格同构，只是基数取【声明上界】）：
 *   cost(field) = multiplier
 *               + Σ cost(child selection)
 *   list 字段   = multiplier
 *               + 上界 × ( 每元素子选择成本之和 )
 *
 * 关键约束：
 *  - 片段重复展开不能绕过预算：每个 spread 出现点都完整计费一次；
 *  - 未知列表规模使用 declaredUpperBound，绝不查询真实行数；
 *  - 查询字符数不计入成本；
 *  - 输出逐字段成本树，与实际成本树结构一致、可逐节点对比。
 */
import type {
  CostNode,
  SchemaDocument,
  SelectionDef,
  VariableValues,
} from '../contract/types.js';
import { findField, isPrimitive } from '../schema/schema.js';
import { isVarRef } from '../query/parser.js';
import type { ValidatedPlan } from '../query/validator.js';

export interface EstimateResult {
  costTree: CostNode;
  total: number;
}

export function estimateCost(plan: ValidatedPlan): EstimateResult {
  const costTree = estimateSelections(
    plan.schema,
    plan.schema.queryType,
    plan.query.selections,
    '$',
    plan.variables,
    [],
  );
  return { costTree, total: costTree.total };
}

function resolveArgs(
  args: Record<string, unknown>,
  variables: Record<string, { value: unknown }>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(args)) {
    out[k] = isVarRef(v) ? variables[v.name]?.value : v;
  }
  return out;
}

/** 估算一组选择（同一对象层级）；返回的 CostNode 是挂在父节点下的占位根 */
function estimateSelections(
  schema: SchemaDocument,
  typeName: string,
  selections: SelectionDef[],
  path: string,
  variables: Record<string, { value: unknown }>,
  fragmentStack: string[],
): CostNode {
  const children: CostNode[] = [];
  let childCost = 0;
  for (const sel of selections) {
    if (sel.kind === 'fragmentSpread') {
      const frag = schema.fragments[sel.name];
      // 片段展开点本身记 0 成本，但其下每个字段照常计费；
      // 同一片段出现 N 次就有 N 棵子树。
      const spreadNode = estimateSelections(
        schema,
        typeName,
        frag.fields,
        path,
        variables,
        [...fragmentStack, frag.name],
      );
      const node: CostNode = {
        path,
        field: `...${frag.name}`,
        selfCost: 0,
        childCost: spreadNode.total,
        total: spreadNode.total,
        viaFragment: frag.name,
        children: spreadNode.children,
      };
      children.push(node);
      childCost += node.total;
      continue;
    }

    const field = findField(schema, typeName, sel.name)!;
    const childPath = `${path}.${sel.alias}`;
    const via = fragmentStack.length > 0 ? fragmentStack[fragmentStack.length - 1] : undefined;
    let node: CostNode;

    if (isPrimitive(field.type)) {
      node = {
        path: childPath,
        field: sel.name,
        selfCost: field.multiplier,
        childCost: 0,
        total: field.multiplier,
        viaFragment: via,
        children: [],
      };
    } else if (field.list) {
      // 列表：字段自身倍率 + 声明上界 × 元素子树
      const elemRoot = estimateSelections(
        schema,
        field.type,
        sel.selections,
        `${childPath}.[]`,
        variables,
        [],
      );
      const perElement = elemRoot.total;
      const childrenTotal = field.declaredUpperBound * perElement;
      node = {
        path: childPath,
        field: sel.name,
        selfCost: field.multiplier,
        childCost: childrenTotal,
        total: field.multiplier + childrenTotal,
        cardinality: { declared: field.declaredUpperBound },
        viaFragment: via,
        children: [
          {
            path: `${childPath}.[]`,
            field: `[per-element ×${field.declaredUpperBound}]`,
            selfCost: 0,
            childCost: childrenTotal,
            total: childrenTotal,
            children: elemRoot.children,
          },
        ],
      };
    } else {
      const sub = estimateSelections(schema, field.type, sel.selections, childPath, variables, []);
      node = {
        path: childPath,
        field: sel.name,
        selfCost: field.multiplier,
        childCost: sub.total,
        total: field.multiplier + sub.total,
        viaFragment: via,
        children: sub.children,
      };
    }
    // 引用参数以保证变量在执行前已解析（成本与字符数无关，但记录基数输入）
    void resolveArgs(sel.args, variables);
    children.push(node);
    childCost += node.total;
  }

  return {
    path,
    field: path === '$' ? '$' : '(selection-set)',
    selfCost: 0,
    childCost,
    total: childCost,
    children,
  };
}
