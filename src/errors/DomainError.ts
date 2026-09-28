/**
 * 领域错误：四类失败必须可区分。
 * 每一类都携带可重放的 runId、路径、关键中间状态与判断理由。
 */
import type { FailureCategory } from '../contract/types.js';

export class DomainError extends Error {
  readonly category: FailureCategory;
  readonly runId: string;
  readonly path?: string;
  readonly context: Record<string, unknown>;

  constructor(
    category: FailureCategory,
    message: string,
    runId: string,
    context: Record<string, unknown> = {},
    path?: string,
  ) {
    super(message);
    this.name = 'DomainError';
    this.category = category;
    this.runId = runId;
    this.context = context;
    this.path = path;
  }

  toLogLine(): string {
    return JSON.stringify({
      level: 'error',
      category: this.category,
      runId: this.runId,
      path: this.path,
      message: this.message,
      context: this.context,
    });
  }
}

export const failureCategories: readonly FailureCategory[] = [
  'INPUT_INVALID',
  'STATE_CONFLICT',
  'RESOURCE_EXHAUSTED',
  'COMPUTATION_FAILED',
];
