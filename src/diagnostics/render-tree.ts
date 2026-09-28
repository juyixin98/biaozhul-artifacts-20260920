/**
 * 逐字段成本树的文本渲染（演示脚本与诊断输出用）。
 */
import type { CostNode } from '../contracts/cost.js';

export function renderCostTree(node: CostNode, indent = 0): string {
  const pad = '  '.repeat(indent);
  const parts: string[] = [];
  const status =
    node.runtimeStatus === 'done'
      ? '✓'
      : node.runtimeStatus === 'aborted'
        ? '✗aborted'
        : '·pending';
  const card =
    node.kind === 'list'
      ? ` bound=${node.cardinality} actual=${node.actualCardinality ?? '-'}`
      : '';
  const actual =
    node.actualSubtree !== undefined ? ` actualSubtree=${node.actualSubtree}` : '';
  parts.push(
    `${pad}${node.label} [est=${node.unitContribution} self=${node.self}${card}${actual} ${status}]`,
  );
  for (const c of node.children) parts.push(renderCostTree(c, indent + 1));
  return parts.join('\n');
}
