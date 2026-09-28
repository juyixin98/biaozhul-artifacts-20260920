/**
 * 诊断运行记录：把每次运行的编号、请求、阶段中间状态、
 * 判断理由与最终结果以 JSONL 追加落盘，并保留内存索引便于回放。
 */
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import type { PhaseEvent } from '../engine/engine.js';
import type { ExecutionResult } from '../contract/types.js';

export interface RunRecord {
  runId: string;
  startedAt: string;
  request: { query: string; variables: unknown; budget: number };
  phases: Array<PhaseEvent & { at: string }>;
  outcome:
    | { kind: 'RESULT'; result: ExecutionResult }
    | { kind: 'ERROR'; category: string; phase: string; message: string; path?: string; context: unknown }
    | { kind: 'UNEXPECTED'; message: string };
  verdict: string;
}

export class RunStore implements Disposable {
  private readonly records = new Map<string, RunRecord>();
  private readonly order: string[] = [];

  constructor(private readonly logPath?: string) {
    if (logPath) {
      const dir = dirname(logPath);
      if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
    }
  }

  start(runId: string, request: RunRecord['request']): RunRecord {
    const rec: RunRecord = {
      runId,
      startedAt: new Date().toISOString(),
      request,
      phases: [],
      outcome: { kind: 'UNEXPECTED', message: 'in-flight' },
      verdict: 'in-flight',
    };
    this.records.set(runId, rec);
    this.order.push(runId);
    return rec;
  }

  phase(runId: string, event: PhaseEvent): void {
    const rec = this.records.get(runId);
    if (rec) rec.phases.push({ ...event, at: new Date().toISOString() });
  }

  finish(runId: string, outcome: RunRecord['outcome'], verdict: string): void {
    const rec = this.records.get(runId);
    if (!rec) return;
    rec.outcome = outcome;
    rec.verdict = verdict;
    this.persist(rec);
  }

  get(runId: string): RunRecord | undefined {
    return this.records.get(runId);
  }

  list(): RunRecord[] {
    return this.order.map((id) => this.records.get(id)!).filter(Boolean);
  }

  /** 从历史 JSONL 读取（用于离线重放/排查） */
  static readLog(path: string): RunRecord[] {
    if (!existsSync(path)) return [];
    return readFileSync(path, 'utf8')
      .split('\n')
      .filter((l) => l.trim().length > 0)
      .map((l) => JSON.parse(l) as RunRecord);
  }

  private persist(rec: RunRecord): void {
    if (!this.logPath) return;
    appendFileSync(this.logPath, `${JSON.stringify(rec)}\n`);
  }

  [Symbol.dispose](): void { /* 文件句柄按行刷盘，无需关闭 */ }
}
