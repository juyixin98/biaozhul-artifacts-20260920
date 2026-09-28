/**
 * 执行内核。
 *
 * 与估计器同构，但基数取适配器给出的【真实元素数】：
 *   每个字段在解析前先向 BudgetLedger.charge 扣减倍率；
 *   列表逐元素执行、逐元素扣减，超限时：
 *     - 当前未完成的元素/字段解析立即取消（异常沿调用栈展开）；
 *     - 已完成的数据与成本节点已就地写入结果容器，随 PARTIAL 返回；
 *     - 列表保留已完成前缀，cancellation 标注触发路径与已扣减成本；
 *     - 成本树在输出时按结构汇总，合计精确等于已扣减成本。
 *
 * 片段每一次 spread 都独立计费；重复别名在响应中合并到同一键，
 * 但成本树保留每次出现（路径带 #dupN），重复不能绕过预算。
 */
import type { CostNode, SchemaDocument, SelectionDef } from '../contract/types.js';
import { findField, isPrimitive } from '../schema/schema.js';
import { isVarRef } from '../query/parser.js';
import type { ValidatedPlan } from '../query/validator.js';
import { BudgetExceeded, BudgetLedger } from './ledger.js';
import type { Row, StateAdapter } from '../state/adapter.js';

interface WorkNode {
  path: string;
  field: string;
  selfCost: number;
  declared?: number;
  actual?: number;
  viaFragment?: string;
  incomplete?: boolean;
  children: WorkNode[];
}

export interface KernelOutput {
  status: 'COMPLETE' | 'PARTIAL';
  data: Record<string, unknown>;
  costTree: CostNode;
  actualCost: number;
  cancellation?: {
    path: string;
    fragment?: string;
    spent: number;
    budget: number;
    reason: string;
  };
}

export function executePlan(plan: ValidatedPlan, adapter: StateAdapter, budget: number): KernelOutput {
  const ledger = new BudgetLedger(budget);
  const rootData: Record<string, unknown> = {};
  const rootNode: WorkNode = { path: '$', field: '$', selfCost: 0, children: [] };

  try {
    runSelectionSet(
      plan.schema,
      plan.schema.queryType,
      plan.query.selections,
      adapter.root(),
      '$',
      rootData,
      rootNode,
      ledger,
      adapter,
      plan.variables,
      [],
      new Map(),
    );
  } catch (err) {
    if (!(err instanceof BudgetExceeded)) throw err;
    const costTree = toCostNode(rootNode);
    return {
      status: 'PARTIAL',
      data: rootData,
      costTree,
      actualCost: ledger.spent,
      cancellation: {
        path: err.path,
        fragment: err.fragment,
        spent: err.spent,
        budget: err.budget,
        reason: err.message,
      },
    };
  }

  const costTree = toCostNode(rootNode);
  return {
    status: 'COMPLETE',
    data: rootData,
    costTree,
    actualCost: ledger.spent,
  };
}

function runSelectionSet(
  schema: SchemaDocument,
  typeName: string,
  selections: SelectionDef[],
  parent: Row,
  path: string,
  data: Record<string, unknown>,
  parentNode: WorkNode,
  ledger: BudgetLedger,
  adapter: StateAdapter,
  variables: Record<string, { value: unknown }>,
  fragmentStack: string[],
  occurrences: Map<string, number>,
): void {
  for (const sel of selections) {
    if (sel.kind === 'fragmentSpread') {
      const frag = schema.fragments[sel.name];
      const spreadNode: WorkNode = {
        path,
        field: `...${frag.name}`,
        selfCost: 0,
        viaFragment: frag.name,
        children: [],
      };
      parentNode.children.push(spreadNode);
      runSelectionSet(
        schema,
        typeName,
        frag.fields,
        parent,
        path,
        data,
        spreadNode,
        ledger,
        adapter,
        variables,
        [...fragmentStack, frag.name],
        new Map(),
      );
      continue;
    }

    const field = findField(schema, typeName, sel.name)!;
    const seen = (occurrences.get(sel.alias) ?? 0) + 1;
    occurrences.set(sel.alias, seen);
    const dupSuffix = seen > 1 ? `#dup${seen}` : '';
    const childPath = `${path}.${sel.alias}${dupSuffix}`;
    const fragmentName = fragmentStack.length > 0 ? fragmentStack[fragmentStack.length - 1] : undefined;

    // 预算门：先扣减，再解析。扣减失败即取消，不会进入适配器。
    ledger.charge(childPath, field.multiplier, `field ${typeName}.${sel.name}`, fragmentName);

    const node: WorkNode = {
      path: childPath,
      field: sel.name,
      selfCost: field.multiplier,
      viaFragment: fragmentName,
      children: [],
    };
    parentNode.children.push(node);

    const args = resolveArgs(sel.args, variables);

    if (isPrimitive(field.type)) {
      data[sel.alias] = adapter.resolveScalar({ parent, typeName, field, args, path: childPath });
      continue;
    }

    if (field.list) {
      node.declared = field.declaredUpperBound;
      node.actual = 0;
      // 数组就地挂到响应键：取消时已完成前缀天然保留。
      const arr: unknown[] = [];
      data[sel.alias] = arr;
      const elems = adapter.iterateList({ parent, typeName, field, args, path: childPath });
      let i = 0;
      for (const row of elems) {
        const elemPath = `${childPath}.${i}`;
        const elemNode: WorkNode = { path: elemPath, field: `[element ${i}]`, selfCost: 0, children: [] };
        const elemData: Record<string, unknown> = {};
        try {
          runSelectionSet(
            schema,
            field.type,
            sel.selections,
            row,
            elemPath,
            elemData,
            elemNode,
            ledger,
            adapter,
            variables,
            [],
            new Map(),
          );
        } catch (inner) {
          if (inner instanceof BudgetExceeded) {
            // 未完成元素：保留部分成本节点并标注，数据侧不含该元素。
            elemNode.incomplete = true;
            node.children.push(elemNode);
            node.actual = i;
          }
          throw inner;
        }
        node.children.push(elemNode);
        arr.push(elemData);
        node.actual = i + 1;
        i++;
      }
      continue;
    }

    const row = adapter.resolveObject({ parent, typeName, field, args, path: childPath });
    if (row === null) {
      data[sel.alias] = null;
    } else {
      const childData: Record<string, unknown> = {};
      runSelectionSet(
        schema,
        field.type,
        sel.selections,
        row,
        childPath,
        childData,
        node,
        ledger,
        adapter,
        variables,
        [],
        new Map(),
      );
      data[sel.alias] = childData;
    }
  }
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

/** 按结构汇总：每个节点 total = selfCost + Σ child.total；根合计即已扣减成本。 */
function toCostNode(n: WorkNode): CostNode {
  const children = n.children.map(toCostNode);
  const childCost = children.reduce((acc, c) => acc + c.total, 0);
  return {
    path: n.path,
    field: n.field,
    selfCost: n.selfCost,
    childCost,
    total: n.selfCost + childCost,
    cardinality: n.declared !== undefined ? { declared: n.declared, actual: n.actual } : undefined,
    viaFragment: n.viaFragment,
    incomplete: n.incomplete,
    children,
  };
}
