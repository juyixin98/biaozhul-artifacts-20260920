/**
 * 诊断记录：每个 GraphQL 请求留下一条结构化决策记录。
 *
 * decision 三态对应需求中的"接受、拒绝、无法判定"：
 * - ACCEPTED    ：通过解析/校验并进入执行（响应中仍可能含字段级错误与部分数据）
 * - REJECTED    ：语法/校验/变量类型等执行前失败，拒绝执行，data 为 null
 * - UNDECIDABLE ：请求信封本身无法判定（非 JSON、缺少 query、operationName 不匹配等）
 *
 * 记录保存在有界环形缓冲中（默认 200 条），可选同步落盘到 LOG_FILE；
 * 敏感信息只以脱敏后的形态进入记录。
 */
import { appendFileSync } from 'node:fs';

export type Decision = 'ACCEPTED' | 'REJECTED' | 'UNDECIDABLE';

export type DiagnosticPhase =
  | 'envelope'
  | 'parse'
  | 'validate'
  | 'coerce-variables'
  | 'execute'
  | 'fatal';

export interface DiagnosticEntry {
  requestId: string;
  timestamp: string;
  durationMs: number;
  decision: Decision;
  phase: DiagnosticPhase;
  /** 机器可读的拒绝/接受原因（错误类别集合或 OK） */
  reasons: string[];
  /** 人类可读的简短说明 */
  summary: string;
  operationName: string | null;
  operationType: 'query' | 'mutation' | null;
  /** 变量形态概览（如 id -> "string"），不含真实值 */
  variableShapes: Record<string, string>;
  fieldErrorCount: number;
  httpStatus: number;
}

export interface DiagnosticSink {
  emit(entry: DiagnosticEntry): void;
}

export class RingBufferSink implements DiagnosticSink {
  private readonly buffer: DiagnosticEntry[] = [];
  private cursor = 0;
  private readonly file: string | null;

  constructor(
    private readonly capacity: number,
    file: string | null = null,
  ) {
    this.file = file;
  }

  emit(entry: DiagnosticEntry): void {
    if (this.buffer.length < this.capacity) {
      this.buffer.push(entry);
    } else {
      this.buffer[this.cursor] = entry;
      this.cursor = (this.cursor + 1) % this.capacity;
    }
    if (this.file) {
      appendFileSync(this.file, `${JSON.stringify(entry)}\n`);
    }
  }

  /** 按时间顺序返回最近条目（环形缓冲内部是环绕的） */
  recent(limit = 50): DiagnosticEntry[] {
    const ordered =
      this.buffer.length < this.capacity
        ? [...this.buffer]
        : [...this.buffer.slice(this.cursor), ...this.buffer.slice(0, this.cursor)];
    return ordered.slice(-limit);
  }

  byRequestId(requestId: string): DiagnosticEntry | undefined {
    return this.recent(this.capacity).find((entry) => entry.requestId === requestId);
  }
}

export class DiagnosticLogger {
  constructor(private readonly sink: DiagnosticSink) {}

  record(entry: DiagnosticEntry): void {
    this.sink.emit(entry);
  }
}
