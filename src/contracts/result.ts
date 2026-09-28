/**
 * 执行结果契约。
 *
 * status:
 * - ok                 完整执行
 * - partial            运行中预算超限，部分结果 + 取消信息
 * - rejected_static    静态估计超预算，从未开始执行
 * - failed             其它错误（输入/状态/计算）
 */
import type { CostNode } from './cost.js';
import type { Diagnostic } from './errors.js';

export type RunStatus = 'ok' | 'partial' | 'rejected_static' | 'failed';

export interface AbortInfo {
  code: 'BUDGET_EXCEEDED_RUNTIME';
  category: 'RESOURCE_EXHAUSTED';
  message: string;
  /** 触发取消的字段路径。 */
  at: string[];
  budget: number;
  /** 取消时已扣减的实际成本。 */
  consumed: number;
}

export interface RunResult {
  runId: string;
  status: RunStatus;
  budget: number;
  estimatedTotal: number;
  /** 实际扣减（partial 时是取消瞬间的值；ok 时等于实际数据成本）。 */
  consumed: number;
  data: unknown;
  /** 逐字段成本树（静态估计 + 运行时实际基数/成本回填）。 */
  costTree: CostNode | null;
  abort?: AbortInfo;
  error?: Diagnostic;
  /** 非致命告警，如运行时实际基数超过声明上界（静态估计偏乐观）。 */
  warnings: BudgetWarning[];
  /** 预算扣减轨迹。 */
  ledger: unknown[];
}

export interface BudgetWarning {
  code: 'DECLARED_BOUND_EXCEEDED';
  /** 出现实际上界超限的列表路径。 */
  path: string[];
  declaredUpperBound: number;
  actualCardinality: number;
}
