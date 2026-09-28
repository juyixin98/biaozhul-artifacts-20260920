/**
 * 运行日志（诊断接口的数据来源）。
 *
 * 每次运行落一条 JSONL 记录，包含可重放问题的三要素：
 * - runId：运行编号；
 * - phases：关键中间状态（解析/展开/估计/网关判定/扣减/取消）；
 * - decisionReason：判断理由（为什么接受、静态拒绝或运行时取消）。
 * 同时在内存保留环形缓冲，供 GET /runs/:id 诊断接口读取。
 */
import { mkdirSync, appendFileSync } from 'node:fs';
import { dirname } from 'node:path';

export interface RunLogEntry {
  runId: string;
  startedAt: string;
  budget: number;
  query: unknown;
  phases: Array<{ phase: string; at: string; detail?: unknown }>;
  estimatedTotal?: number;
  decisionReason?: string;
  finalStatus?: string;
  consumed?: number;
  ledger?: unknown[];
  error?: unknown;
}

const MAX_MEMORY_ENTRIES = 500;

export class RunLogger {
  private readonly memory: RunLogEntry[] = [];
  private readonly filePath: string | null;
  private counter = 0;

  constructor(filePath: string | null = 'logs/runs.jsonl') {
    this.filePath = filePath;
  }

  newRun(budget: number, query: unknown): RunLogEntry {
    this.counter += 1;
    const entry: RunLogEntry = {
      runId: makeRunId(this.counter),
      startedAt: new Date().toISOString(),
      budget,
      query,
      phases: [],
    };
    return entry;
  }

  phase(entry: RunLogEntry, phase: string, detail?: unknown): void {
    entry.phases.push({ phase, at: new Date().toISOString(), detail });
  }

  /** 落盘并进入内存环形缓冲。 */
  commit(entry: RunLogEntry): void {
    this.memory.push(entry);
    if (this.memory.length > MAX_MEMORY_ENTRIES) this.memory.shift();
    if (this.filePath) {
      try {
        mkdirSync(dirname(this.filePath), { recursive: true });
        appendFileSync(this.filePath, `${JSON.stringify(entry)}\n`);
      } catch {
        // 日志落盘失败不应吞掉运行本身；诊断接口仍可从内存读到本次记录。
      }
    }
  }

  get(runId: string): RunLogEntry | undefined {
    return this.memory.find((e) => e.runId === runId);
  }

  list(): RunLogEntry[] {
    return [...this.memory].reverse();
  }
}

function makeRunId(counter: number): string {
  const ts = Date.now().toString(36);
  const rand = Math.random().toString(36).slice(2, 8);
  return `run_${ts}_${counter.toString(36)}_${rand}`;
}
