/**
 * 诊断记录器：
 *  - 每个请求生成 requestId，记录 decision(accepted/rejected/indeterminate) 与原因码；
 *  - 保留关键状态（操作类型/名称、变量形状、错误类别、耗时）；
 *  - 内存环形缓冲（默认 200 条），供 GET /diagnostics 查询；
 *  - 可选输出到 stderr 的结构化日志，值全部脱敏。
 */

import { randomUUID } from 'node:crypto';
import { redactErrorObject, redactVariables } from './redact.js';

export type Decision = 'accepted' | 'rejected' | 'indeterminate';

export interface DiagnosisEntry {
  requestId: string;
  timestamp: string;
  decision: Decision;
  reasons: string[];
  operationType: string | null;
  operationName: string | null;
  variableShape: Record<string, string>;
  errorCategories: string[];
  errorCount: number;
  durationMs: number;
  /** 人类可读的判定说明 */
  explanation: string;
  errors?: Array<{
    message: string;
    path?: ReadonlyArray<string | number>;
    category: string;
  }>;
}

export interface RecordInput {
  decision: Decision;
  reasons: string[];
  operationType: string | null;
  operationName: string | null;
  variables: Record<string, unknown> | null;
  errors: Array<{
    message: string;
    path?: ReadonlyArray<string | number>;
    category?: string;
  }>;
  durationMs: number;
}

export class DiagnosticCollector {
  private readonly entries: DiagnosisEntry[] = [];

  constructor(private readonly capacity = 200) {}

  newRequestId(): string {
    return randomUUID();
  }

  record(requestId: string, input: RecordInput): DiagnosisEntry {
    const entry: DiagnosisEntry = {
      requestId,
      timestamp: new Date().toISOString(),
      decision: input.decision,
      reasons: input.reasons,
      operationType: input.operationType,
      operationName: input.operationName,
      variableShape: redactVariables(input.variables),
      errorCategories: [...new Set(input.errors.map((e) => e.category ?? 'INTERNAL'))],
      errorCount: input.errors.length,
      durationMs: input.durationMs,
      explanation: explain(input.decision, input.reasons, input.errors.length),
      errors:
        input.errors.length > 0
          ? input.errors.map((e) => ({
              ...redactErrorObject(e),
              category: e.category ?? 'INTERNAL',
            }))
          : undefined,
    };
    this.entries.push(entry);
    if (this.entries.length > this.capacity) {
      this.entries.splice(0, this.entries.length - this.capacity);
    }
    return entry;
  }

  list(limit = 50): DiagnosisEntry[] {
    return this.entries.slice(-limit).reverse();
  }

  get(requestId: string): DiagnosisEntry | undefined {
    return this.entries.find((e) => e.requestId === requestId);
  }
}

function explain(
  decision: Decision,
  reasons: string[],
  errorCount: number,
): string {
  switch (decision) {
    case 'accepted':
      return errorCount > 0
        ? `请求已执行，但有 ${errorCount} 个字段级错误；已按非空/可空类型边界返回部分数据。`
        : '请求通过解析、校验与变量转换，完整执行。';
    case 'rejected':
      return `请求在执行前被拒绝：${reasons.join(', ')}（未运行任何 resolver）。`;
    case 'indeterminate':
      return `无法判定执行目标：${reasons.join(', ')}（例如缺少/歧义的 operation 名称）。`;
  }
}

/** 结构化单行日志（stderr），供本地开发观察；敏感信息已脱敏。 */
export function emitStructuredLog(entry: DiagnosisEntry): void {
  const line = JSON.stringify({
    requestId: entry.requestId,
    ts: entry.timestamp,
    decision: entry.decision,
    reasons: entry.reasons,
    operation: entry.operationName
      ? `${entry.operationType ?? '?'}/${entry.operationName}`
      : entry.operationType,
    variables: entry.variableShape,
    errorCategories: entry.errorCategories,
    errorCount: entry.errorCount,
    durationMs: entry.durationMs,
  });
  process.stderr.write(`[diagnostics] ${line}\n`);
}
