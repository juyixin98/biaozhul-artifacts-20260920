/**
 * 执行内核共享上下文：变量表 + 预算账本 + 取消信号。
 *
 * 取消传播模型：
 * - charge() 在余额不足时把 aborted 置为 true 并抛出 BudgetAbortError；
 * - 内核在每个解析器入口检查 aborted，未启动的解析器不再执行；
 * - 已经在深层嵌套中的解析器随异常向上冒泡被跳过，已产出的部分结果保留。
 */
import type { VarValue } from '../contracts/ast.js';
import { fail } from '../contracts/errors.js';

/** 运行中预算耗尽时抛出；区别于静态拒绝（不经过账本）。 */
export class BudgetAbortError extends Error {
  readonly consumed: number;
  readonly budget: number;
  constructor(consumed: number, budget: number, at: string) {
    super(`runtime budget exhausted at "${at}": consumed ${consumed} / budget ${budget}`);
    this.name = 'BudgetAbortError';
    this.consumed = consumed;
    this.budget = budget;
  }
}

export interface ChargedStep {
  at: string;
  amount: number;
  /** 扣减后的累计已消耗。 */
  consumedAfter: number;
  remaining: number;
  rejected: boolean;
}

export class RunContext {
  readonly vars: ReadonlyMap<string, VarValue>;
  readonly budget: number;
  consumed = 0;
  aborted = false;
  /** 扣减轨迹：重放问题时的关键中间状态。 */
  readonly ledger: ChargedStep[] = [];

  constructor(vars: ReadonlyMap<string, VarValue>, budget: number) {
    this.vars = vars;
    this.budget = budget;
  }

  /**
   * 扣减成本。余额不足时：先记录被拒绝的一步（便于重放），
   * 再翻转 aborted 并抛出 BudgetAbortError。
   */
  charge(amount: number, at: string): void {
    if (this.aborted) {
      // 取消已发生：任何迟到的解析器立即止步，不再产生新工作。
      throw new BudgetAbortError(this.consumed, this.budget, at);
    }
    const next = this.consumed + amount;
    if (next > this.budget) {
      this.aborted = true;
      this.ledger.push({
        at,
        amount,
        consumedAfter: this.consumed,
        remaining: this.budget - this.consumed,
        rejected: true,
      });
      throw new BudgetAbortError(next, this.budget, at);
    }
    this.consumed = next;
    this.ledger.push({
      at,
      amount,
      consumedAfter: next,
      remaining: this.budget - next,
      rejected: false,
    });
  }

  checkAborted(at: string): void {
    if (this.aborted) {
      throw new BudgetAbortError(this.consumed, this.budget, at);
    }
  }

  /** 包装适配器错误：基础设施故障归类 COMPUTATION_FAILED。 */
  static toStoreFailure(cause: unknown, path: string[]): never {
    const message = cause instanceof Error ? cause.message : String(cause);
    fail(
      'COMPUTATION_FAILED',
      'STORE_FAILURE',
      `state adapter failure at "${path.join(' / ')}": ${message}`,
      path,
      { cause: message },
    );
  }
}
