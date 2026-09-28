/**
 * 统一错误契约。
 *
 * 四类失败必须可区分（README“错误语义”详述）：
 * - INPUT_ERROR    输入错误：结构/类型/未知名/重复别名/空选择等
 * - STATE_CONFLICT 状态冲突：执行前数据前置条件不成立（如根对象不存在）
 * - RESOURCE_EXHAUSTED 资源耗尽：静态估计或运行中实际成本超过预算
 * - COMPUTATION_FAILED 计算失败：状态适配层等基础设施错误
 *
 * 另设 ABORTED 表示运行中被取消传播时“未完成解析器”的逐节点状态
 * （它不是独立的顶层失败类别——顶层一定是 RESOURCE_EXHAUSTED）。
 */
export type ErrorCategory =
  | 'INPUT_ERROR'
  | 'STATE_CONFLICT'
  | 'RESOURCE_EXHAUSTED'
  | 'COMPUTATION_FAILED'
  | 'ABORTED';

export type ErrorCode =
  // 输入错误
  | 'MALFORMED_QUERY'
  | 'UNKNOWN_TYPE'
  | 'UNKNOWN_FIELD'
  | 'UNKNOWN_RELATION'
  | 'UNKNOWN_FRAGMENT'
  | 'INLINE_FRAGMENT_FORBIDDEN'
  | 'DUPLICATE_ALIAS'
  | 'EMPTY_SELECTION'
  | 'FRAGMENT_CYCLE'
  | 'DUPLICATE_VAR'
  | 'UNKNOWN_VAR'
  | 'VAR_TYPE_MISMATCH'
  | 'VAR_ARG_TYPE_MISMATCH'
  | 'INVALID_BOUND'
  // 状态冲突
  | 'ROOT_NOT_FOUND'
  // 资源耗尽
  | 'BUDGET_EXCEEDED_STATIC'
  | 'BUDGET_EXCEEDED_RUNTIME'
  // 计算失败
  | 'STORE_FAILURE';

export interface Diagnostic {
  category: ErrorCategory;
  code: ErrorCode;
  message: string;
  /** 出错字段路径，如 "users / orgUsers"。 */
  path?: string[];
  /** 便于断言与排障的附加上下文。 */
  details?: Record<string, unknown>;
}

export class QueryError extends Error {
  readonly diagnostic: Diagnostic;

  constructor(diagnostic: Diagnostic) {
    super(diagnostic.message);
    this.name = 'QueryError';
    this.diagnostic = diagnostic;
  }
}

export function fail(
  category: ErrorCategory,
  code: ErrorCode,
  message: string,
  path?: string[],
  details?: Record<string, unknown>,
): never {
  throw new QueryError({ category, code, message, path, details });
}
