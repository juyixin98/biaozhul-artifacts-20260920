/**
 * 逐字段成本树文本渲染：估计树与实际树同构，可逐节点对比。
 */
import type { CostNode } from '../contract/types.js';

export function renderCostTree(root: CostNode, opts: { indentStep?: string } = {}): string {
  const step = opts.indentStep ?? '  ';
  const lines: string[] = [];
  walk(root, '', true, true);
  return lines.join('\n');

  function walk(n: CostNode, prefix: string, isLast: boolean, isRoot: boolean): void {
    const branch = isRoot ? '' : `${isLast ? '└─ ' : '├─ '}`;
    const card = n.cardinality
      ? n.cardinality.actual !== undefined
        ? ` [card declared=${n.cardinality.declared} actual=${n.cardinality.actual}]`
        : ` [card declared=${n.cardinality.declared}]`
      : '';
    const frag = n.viaFragment ? ` via ...${n.viaFragment}` : '';
    const incomplete = n.incomplete ? '  <<INCOMPLETE (cancelled)>>' : '';
    lines.push(
      `${prefix}${branch}${n.field}  self=${n.selfCost} children=${n.childCost} total=${n.total}${card}${frag}${incomplete}`,
    );
    const childPrefix = isRoot ? '' : `${prefix}${isLast ? step : '│ '}`;
    n.children.forEach((c, i) => walk(c, childPrefix, i === n.children.length - 1, false));
  }
}

export function flattenCost(root: CostNode): Array<{ path: string; total: number; selfCost: number }> {
  const out: Array<{ path: string; total: number; selfCost: number }> = [];
  const walk = (n: CostNode): void => {
    out.push({ path: n.path, total: n.total, selfCost: n.selfCost });
    n.children.forEach(walk);
  };
  walk(root);
  return out;
}
