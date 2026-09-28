/**
 * 运行时预算账本：逐笔扣减，超预算立即拒绝该笔并抛出 BudgetExceeded。
 * 账本是单一预算门：所有解析器在产生成本前必须先 charge。
 */
export interface ChargeRecord {
  seq: number;
  path: string;
  amount: number;
  spentAfter: number;
  fragment?: string;
  reason: string;
}

export class BudgetExceeded extends Error {
  readonly path: string;
  readonly spent: number;
  readonly attempted: number;
  readonly budget: number;
  readonly fragment?: string;
  readonly lastCharges: ChargeRecord[];

  constructor(params: {
    path: string;
    spent: number;
    attempted: number;
    budget: number;
    fragment?: string;
    lastCharges: ChargeRecord[];
  }) {
    super(
      `runtime budget exceeded at ${params.path}: spending ${params.spent} + ${params.attempted} > budget ${params.budget}`,
    );
    this.name = 'BudgetExceeded';
    this.path = params.path;
    this.spent = params.spent;
    this.attempted = params.attempted;
    this.budget = params.budget;
    this.fragment = params.fragment;
    this.lastCharges = params.lastCharges;
  }
}

export class BudgetLedger {
  private spentValue = 0;
  private seq = 0;
  readonly charges: ChargeRecord[] = [];

  constructor(readonly budget: number) {}

  get spent(): number { return this.spentValue; }
  get remaining(): number { return this.budget - this.spentValue; }

  charge(path: string, amount: number, reason: string, fragment?: string): void {
    if (this.spentValue + amount > this.budget) {
      throw new BudgetExceeded({
        path,
        spent: this.spentValue,
        attempted: amount,
        budget: this.budget,
        fragment,
        lastCharges: this.charges.slice(-5),
      });
    }
    this.spentValue += amount;
    const record: ChargeRecord = {
      seq: (this.seq += 1),
      path,
      amount,
      spentAfter: this.spentValue,
      fragment,
      reason,
    };
    this.charges.push(record);
  }
}
