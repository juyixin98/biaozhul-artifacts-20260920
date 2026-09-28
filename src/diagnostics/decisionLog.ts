/**
 * 诊断记录：每个对象读取请求一条 DecisionRecord，说明
 * “接受 / 拒绝 / 无法判定”的结论、原因与关键状态。
 * 存于固定容量环形缓冲（默认 500 条），可经 HTTP 诊断接口读取。
 */

export interface DecisionRecord {
  readonly requestId: string;
  readonly timestamp: string;
  readonly method: string;
  readonly path: string;
  readonly objectId: string | null;
  readonly objectSize: string | null;
  /** 白名单过滤并脱敏后的请求头。 */
  readonly requestHeaders: Record<string, string>;
  readonly outcome: 'full' | 'partial' | 'rejected' | 'not-found' | 'error';
  readonly statusCode: number;
  /** 内核给出的机器可读原因码，如 if-range-mismatch / none-satisfiable。 */
  readonly reason: string | null;
  readonly detail: string | null;
  readonly multipart: boolean | null;
  readonly intervals: ReadonlyArray<{ readonly start: string; readonly end: string }>;
  readonly parsedSpecCount: number | null;
  readonly satisfiableCount: number | null;
  readonly unsatisfiableCount: number | null;
  readonly unsatisfiableReasons: readonly string[];
  readonly plannedTotalBytes: string | null;
  /** 实际写出的体字节；用于与 Content-Length 交叉核验。 */
  readonly actualBodyBytes: string | null;
  readonly contentLengthHeader: string | null;
  readonly headerBodyConsistent: boolean | null;
}

export interface ListOptions {
  readonly limit?: number;
}

/** 固定容量环形缓冲；满了覆盖最旧记录，避免诊断内存无限增长。 */
export class DecisionLog {
  private readonly records: DecisionRecord[] = [];
  private cursor = 0;
  private readonly byId = new Map<string, number>();

  constructor(private readonly capacity: number) {
    if (capacity <= 0) throw new Error('诊断缓冲容量必须为正整数');
  }

  record(entry: DecisionRecord): void {
    if (this.records.length < this.capacity) {
      const index = this.records.length;
      this.records.push(entry);
      this.byId.set(entry.requestId, index);
    } else {
      const index = this.cursor;
      const old = this.records[index];
      if (old) this.byId.delete(old.requestId);
      this.records[index] = entry;
      this.byId.set(entry.requestId, index);
      this.cursor = (this.cursor + 1) % this.capacity;
    }
  }

  get(requestId: string): DecisionRecord | null {
    const index = this.byId.get(requestId);
    return index === undefined ? null : (this.records[index] ?? null);
  }

  /** 按时间从新到旧返回。 */
  list(options: ListOptions = {}): DecisionRecord[] {
    const limit = Math.min(options.limit ?? this.capacity, this.capacity);
    const ordered: DecisionRecord[] = [];
    for (let i = 0; i < this.records.length; i++) {
      const index = (this.cursor + i) % this.capacity;
      // 容量未满时，后半段是空洞。
      if (index < this.records.length && this.records[index]) {
        ordered.push(this.records[index]!);
      }
    }
    return ordered.reverse().slice(0, limit);
  }

  get size(): number {
    return this.records.length;
  }
}
