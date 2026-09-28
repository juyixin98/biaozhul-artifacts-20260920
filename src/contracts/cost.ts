/**
 * 成本契约。
 *
 * 成本三要素（均与查询字符数无关）：
 * 1. 片段展开 —— 每次 spread/include 都把片段字段计入，重复不抵消；
 * 2. 列表基数 —— 未知规模采用声明上界，进入列表时父成本按基数相乘；
 * 3. 字段倍率 —— 叶子字段成本 = multiplier（重字段更贵）。
 *
 * CostNode 既是“静态成本树”的节点，也在运行中被实际成本回填，
 * 因此同一棵树可以同时展示估计值与实际值（逐字段成本树）。
 */

export interface CostNode {
  /** 可读标签，如 "field:name"、"list:users"、"spread:pageInfo"。 */
  label: string;
  kind: 'field' | 'list' | 'spread' | 'root';
  /** 该节点自身的叶子成本（字段倍率；非叶子为 0）。 */
  self: number;
  /**
   * 该节点相对根的单位贡献（静态）：
   * 已乘上从根到该节点的全部列表基数上界。整棵树 estimatedTotal
   * 等于所有节点 unitContribution 之和。
   */
  unitContribution: number;
  /** 列表使用的基数上界（仅 list 节点）。 */
  cardinality?: number;
  children: CostNode[];
  /**
   * 运行时回填：每个解析器的状态。
   * pending 尚未执行；done 完整完成；aborted 因预算取消而未完成
   * （用于在逐字段成本树与部分结果中标注未完成解析器）。
   */
  runtimeStatus: 'pending' | 'done' | 'aborted';
  /** 运行时回填：该节点实际遍历的基数（仅 list 节点）。 */
  actualCardinality?: number;
  /** 运行时回填：该节点实际发生的成本（未乘父级，见 actualTree 汇总）。 */
  actualSelf?: number;
  /** 运行时回填：该子树实际总成本（已按真实基数汇总）。 */
  actualSubtree?: number;
}

export interface BudgetDecision {
  accepted: boolean;
  estimatedTotal: number;
  budget: number;
}
