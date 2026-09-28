/**
 * Synthetic local fixtures: the only "business data" in the service.
 * No external accounts, no network. Balances live in memory for the lifetime
 * of the process and reset on restart — explicitly a fixture, not a database.
 */

export interface Account {
  readonly id: string;
  readonly owner: string;
  balance: number;
  readonly currency: "CNY";
}

const INITIAL_ACCOUNTS: ReadonlyArray<Account> = [
  { id: "acc-1", owner: "alice (fixture)", balance: 100_000, currency: "CNY" },
  { id: "acc-2", owner: "bob (fixture)", balance: 50_000, currency: "CNY" },
  { id: "acc-3", owner: "carol (fixture)", balance: 0, currency: "CNY" },
];

export class InsufficientFundsError extends Error {
  constructor(
    readonly accountId: string,
    readonly balance: number,
    readonly requested: number,
  ) {
    super(
      `account ${accountId} has balance ${balance}, cannot debit ${requested}`,
    );
    this.name = "InsufficientFundsError";
  }
}

export class UnknownAccountError extends Error {
  constructor(readonly accountId: string) {
    super(`unknown fixture account: ${accountId}`);
    this.name = "UnknownAccountError";
  }
}

export class FixtureAccounts {
  private readonly accounts = new Map<string, Account>();

  constructor(seed: ReadonlyArray<Account> = INITIAL_ACCOUNTS) {
    for (const account of seed) {
      // Copy so external mutation of the seed cannot affect balances.
      this.accounts.set(account.id, { ...account });
    }
  }

  exists(accountId: string): boolean {
    return this.accounts.has(accountId);
  }

  getBalance(accountId: string): number {
    const account = this.accounts.get(accountId);
    if (!account) throw new UnknownAccountError(accountId);
    return account.balance;
  }

  list(): ReadonlyArray<Account> {
    return [...this.accounts.values()].map((a) => ({ ...a }));
  }

  /**
   * Atomic (synchronous) debit/credit. No await between check and mutation,
   * so concurrent transfers cannot interleave a stale balance check.
   */
  transfer(from: string, to: string, amount: number): {
    fromBalance: number;
    toBalance: number;
  } {
    const source = this.accounts.get(from);
    const target = this.accounts.get(to);
    if (!source) throw new UnknownAccountError(from);
    if (!target) throw new UnknownAccountError(to);
    if (source.balance < amount) {
      throw new InsufficientFundsError(from, source.balance, amount);
    }
    source.balance -= amount;
    target.balance += amount;
    return { fromBalance: source.balance, toBalance: target.balance };
  }
}

/**
 * In-memory secret vault fixture: stores an irreversible hash-like token of
 * the secret so tests can prove the plaintext never reaches the ledger.
 */
export class FixtureSecretVault {
  private readonly entries = new Map<
    string,
    { name: string; digest: string; createdAt: string }
  >();

  store(name: string, secret: string, now: Date): string {
    let hash = 0;
    for (let i = 0; i < secret.length; i++) {
      hash = (hash * 31 + secret.charCodeAt(i)) | 0;
    }
    const token = `tok_${(hash >>> 0).toString(16).padStart(8, "0")}`;
    this.entries.set(name, {
      name,
      digest: token,
      createdAt: now.toISOString(),
    });
    return token;
  }

  size(): number {
    return this.entries.size;
  }
}
