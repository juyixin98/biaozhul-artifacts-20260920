/**
 * RPC method registry.
 *
 * Every method is split into:
 *  - validate(params): pure contract check. Only this may raise INVALID_PARAMS.
 *    It runs BEFORE any operation number is allocated, so a rejected call
 *    never leaves a side-effect row.
 *  - execute(params, ctx): the actual work, allowed to raise BusinessError
 *    (business_rule layer) or to perform fixture side effects.
 *
 * Side-effecting methods carry a SideEffectSpec:
 *  - kind            : stable operation type stored on the operations row
 *  - idempotencyKey  : explicit client key extractor. The JSON-RPC id is
 *                      NEVER used as an idempotency key — it is a transport
 *                      correlation value that clients legitimately reuse.
 *  - redactInput     : builds the only representation of the input allowed to
 *                      touch the ledger or logs.
 *
 * mode "async" methods allocate the operation row, return immediately with
 * { accepted, opSeq, status: "pending" } and settle in the background;
 * clients poll operations.get to learn the terminal state.
 */

import {
  BUSINESS_VALUE_OUT_OF_RANGE,
  BusinessError,
  INVALID_PARAMS,
  RpcException,
  SYNTHETIC_BUSINESS_FAILURE,
} from "../protocol/errors.js";
import { REDACTED } from "../util/redact.js";
import {
  FixtureAccounts,
  FixtureSecretVault,
  InsufficientFundsError,
  UnknownAccountError,
} from "./fixtures.js";

export interface MethodContext {
  readonly now: Date;
  readonly accounts: FixtureAccounts;
  readonly vault: FixtureSecretVault;
}

export interface SideEffectSpec {
  readonly kind: string;
  /** Returns null when no explicit key is present; must never throw. */
  readonly idempotencyKey?: (params: Record<string, unknown>) => string | null;
  readonly redactInput: (params: Record<string, unknown>) => unknown;
}

export interface MethodDefinition {
  readonly name: string;
  readonly mode: "sync" | "async";
  readonly sideEffect?: SideEffectSpec;
  validate(params: unknown): Record<string, unknown>;
  execute(
    params: Record<string, unknown>,
    ctx: MethodContext,
  ): unknown | Promise<unknown>;
}

/* ------------------------------- validation ------------------------------ */

function invalidParams(message: string): never {
  throw new RpcException(
    INVALID_PARAMS,
    `Invalid params: ${message}`,
    "invalid_params",
  );
}

function requireParamsObject(params: unknown): Record<string, unknown> {
  if (typeof params !== "object" || params === null || Array.isArray(params)) {
    invalidParams("params must be a JSON object");
  }
  return params as Record<string, unknown>;
}

function requireFiniteNumber(
  obj: Record<string, unknown>,
  key: string,
): number {
  const value = obj[key];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    invalidParams(`${key} must be a finite number`);
  }
  return value;
}

function requireInteger(
  obj: Record<string, unknown>,
  key: string,
  opts: { min?: number; max?: number } = {},
): number {
  const value = obj[key];
  if (typeof value !== "number" || !Number.isInteger(value)) {
    invalidParams(`${key} must be an integer`);
  }
  if (opts.min !== undefined && value < opts.min) {
    invalidParams(`${key} must be >= ${opts.min}`);
  }
  if (opts.max !== undefined && value > opts.max) {
    invalidParams(`${key} must be <= ${opts.max}`);
  }
  return value;
}

function requireString(
  obj: Record<string, unknown>,
  key: string,
  opts: { minLength?: number; maxLength?: number } = {},
): string {
  const value = obj[key];
  if (typeof value !== "string") invalidParams(`${key} must be a string`);
  if (opts.minLength !== undefined && value.length < opts.minLength) {
    invalidParams(`${key} must have length >= ${opts.minLength}`);
  }
  if (opts.maxLength !== undefined && value.length > opts.maxLength) {
    invalidParams(`${key} must have length <= ${opts.maxLength}`);
  }
  return value;
}

function optionalBoolean(
  obj: Record<string, unknown>,
  key: string,
  fallback = false,
): boolean {
  if (!(key in obj)) return fallback;
  const value = obj[key];
  if (typeof value !== "boolean") invalidParams(`${key} must be a boolean`);
  return value;
}

function optionalInteger(
  obj: Record<string, unknown>,
  key: string,
  fallback: number,
  opts: { min?: number; max?: number },
): number {
  if (!(key in obj)) return fallback;
  return requireInteger(obj, key, opts);
}

/**
 * Key-shape check used both by validation (rejects malformed keys) and kept
 * separate from extraction (which stays throw-free for the kernel).
 */
export function readIdempotencyKey(
  obj: Record<string, unknown>,
): string | null {
  if (!("idempotencyKey" in obj)) return null;
  const value = obj.idempotencyKey;
  if (typeof value !== "string" || value.length === 0 || value.length > 128) {
    invalidParams("idempotencyKey must be a non-empty string of <= 128 chars");
  }
  return value;
}

function toBusinessError(cause: unknown): never {
  if (cause instanceof InsufficientFundsError) {
    throw new BusinessError(
      BUSINESS_VALUE_OUT_OF_RANGE,
      "transfer rejected: insufficient funds",
      {
        accountId: cause.accountId,
        balance: cause.balance,
        requested: cause.requested,
      },
    );
  }
  if (cause instanceof UnknownAccountError) {
    throw new BusinessError(-32002, "unknown fixture account", {
      accountId: cause.accountId,
    });
  }
  throw cause;
}

/* --------------------------------- methods -------------------------------- */

const mathAdd: MethodDefinition = {
  name: "math.add",
  mode: "sync",
  validate(params) {
    const obj = requireParamsObject(params);
    return { a: requireFiniteNumber(obj, "a"), b: requireFiniteNumber(obj, "b") };
  },
  execute({ a, b }) {
    return { sum: (a as number) + (b as number), addends: [a, b] };
  },
};

const echo: MethodDefinition = {
  name: "echo",
  mode: "sync",
  // echo accepts ANY valid params (including arrays and absent params); it
  // exists to prove response attribution, so validation must be permissive.
  validate(params) {
    return { value: params === undefined ? null : params };
  },
  execute({ value }) {
    return { echoed: value };
  },
};

const debugSleep: MethodDefinition = {
  name: "debug.sleep",
  mode: "sync",
  validate(params) {
    const obj = requireParamsObject(params);
    return { delayMs: optionalInteger(obj, "delayMs", 20, { min: 0, max: 10_000 }) };
  },
  async execute({ delayMs }) {
    await new Promise((resolve) => setTimeout(resolve, delayMs as number));
    return { slept: delayMs };
  },
};

/**
 * Side-effecting debug method: delays then emits a synthetic journal entry.
 * It is a real side effect (gets an independent operation number) but takes
 * long enough to be interrupted, which makes disconnect handling testable.
 */
const debugSideEffect: MethodDefinition = {
  name: "debug.sideEffect",
  mode: "sync",
  sideEffect: {
    kind: "debug_side_effect",
    idempotencyKey: (p) =>
      typeof p.dedup === "string" && p.dedup.length > 0 && p.dedup.length <= 128
        ? p.dedup
        : null,
    redactInput: (p) => ({ delayMs: p.delayMs, note: p.note }),
  },
  validate(params) {
    const obj = requireParamsObject(params);
    return {
      delayMs: optionalInteger(obj, "delayMs", 20, { min: 0, max: 10_000 }),
      note: "note" in obj ? requireString(obj, "note", { maxLength: 200 }) : null,
      ...("dedup" in obj ? { dedup: readIdempotencyKey(obj) } : {}),
    };
  },
  async execute({ delayMs, note }, ctx) {
    await new Promise((resolve) => setTimeout(resolve, delayMs as number));
    return { journaled: true, note: note ?? null, at: ctx.now.toISOString() };
  },
};

const accountsList: MethodDefinition = {
  name: "accounts.list",
  mode: "sync",
  validate() {
    return {};
  },
  execute(_params, ctx) {
    return { accounts: ctx.accounts.list() };
  },
};

const accountsBalance: MethodDefinition = {
  name: "accounts.balance",
  mode: "sync",
  validate(params) {
    const obj = requireParamsObject(params);
    return { accountId: requireString(obj, "accountId", { minLength: 1 }) };
  },
  execute({ accountId }, ctx) {
    try {
      return { accountId, balance: ctx.accounts.getBalance(accountId as string) };
    } catch (cause) {
      return toBusinessError(cause);
    }
  },
};

const accountsTransfer: MethodDefinition = {
  name: "accounts.transfer",
  mode: "sync",
  sideEffect: {
    kind: "transfer",
    idempotencyKey: (p) =>
      typeof p.idempotencyKey === "string" && p.idempotencyKey.length <= 128
        ? p.idempotencyKey
        : null,
    redactInput: (p) => ({
      from: p.from,
      to: p.to,
      amount: p.amount,
      hasIdempotencyKey: "idempotencyKey" in p,
    }),
  },
  validate(params) {
    const obj = requireParamsObject(params);
    const from = requireString(obj, "from", { minLength: 1 });
    const to = requireString(obj, "to", { minLength: 1 });
    if (from === to) invalidParams("from and to must differ");
    // Fixture amounts are integer cents. Fractional/negative amounts are a
    // contract failure; insufficient funds is a business failure.
    const amount = requireInteger(obj, "amount", { min: 1, max: 1_000_000_000 });
    const idempotencyKey = readIdempotencyKey(obj);
    return { from, to, amount, idempotencyKey };
  },
  execute({ from, to, amount }, ctx) {
    try {
      const balances = ctx.accounts.transfer(
        from as string,
        to as string,
        amount as number,
      );
      return { transferred: amount, from, to, ...balances };
    } catch (cause) {
      return toBusinessError(cause);
    }
  },
};

const secretsPut: MethodDefinition = {
  name: "secrets.put",
  mode: "sync",
  sideEffect: {
    kind: "secret_store",
    idempotencyKey: (p) =>
      typeof p.name === "string" && p.name.length > 0 && p.name.length <= 64
        ? `name:${p.name}`
        : null,
    redactInput: (p) => ({ name: p.name, secret: REDACTED }),
  },
  validate(params) {
    const obj = requireParamsObject(params);
    return {
      name: requireString(obj, "name", { minLength: 1, maxLength: 64 }),
      secret: requireString(obj, "secret", { minLength: 1, maxLength: 1024 }),
    };
  },
  execute({ name, secret }, ctx) {
    const token = ctx.vault.store(name as string, secret as string, ctx.now);
    return { name, stored: true, token };
  },
};

const tasksSchedule: MethodDefinition = {
  name: "tasks.schedule",
  mode: "async",
  sideEffect: {
    kind: "scheduled_task",
    redactInput: (p) => ({ name: p.name, delayMs: p.delayMs, fail: p.fail }),
  },
  validate(params) {
    const obj = requireParamsObject(params);
    return {
      name: requireString(obj, "name", { minLength: 1, maxLength: 64 }),
      delayMs: optionalInteger(obj, "delayMs", 50, { min: 0, max: 5000 }),
      fail: optionalBoolean(obj, "fail", false),
    };
  },
  async execute({ name, delayMs, fail }, ctx) {
    await new Promise((resolve) => setTimeout(resolve, delayMs as number));
    if (fail) {
      throw new BusinessError(
        SYNTHETIC_BUSINESS_FAILURE,
        "scheduled task failed by fixture request",
        { task: name },
      );
    }
    return { task: name, finishedAt: ctx.now.toISOString() };
  },
};

/** Kernel-resolved methods are registered so unknown-method handling is uniform. */
const operationsGet: MethodDefinition = {
  name: "operations.get",
  mode: "sync",
  validate(params) {
    const obj = requireParamsObject(params);
    return { opSeq: requireInteger(obj, "opSeq", { min: 1 }) };
  },
  execute() {
    throw new RpcException(-32603, "Internal error", "internal");
  },
};

const operationsList: MethodDefinition = {
  name: "operations.list",
  mode: "sync",
  validate(params) {
    if (params === undefined) return {};
    const obj = requireParamsObject(params);
    const out: Record<string, unknown> = {};
    if ("status" in obj) {
      const status = obj.status;
      if (
        !["pending", "succeeded", "failed", "skipped", "interrupted"].includes(
          status as string,
        )
      ) {
        invalidParams("status is not a recognized operation status");
      }
      out.status = status;
    }
    if ("kind" in obj) out.kind = requireString(obj, "kind", { maxLength: 64 });
    if ("limit" in obj) {
      out.limit = requireInteger(obj, "limit", { min: 1, max: 500 });
    }
    return out;
  },
  execute() {
    throw new RpcException(-32603, "Internal error", "internal");
  },
};

export const KERNEL_RESOLVED_METHODS = new Set(["operations.get", "operations.list"]);

export function buildMethodRegistry(): Map<string, MethodDefinition> {
  const entries: MethodDefinition[] = [
    mathAdd,
    echo,
    debugSleep,
    debugSideEffect,
    accountsList,
    accountsBalance,
    accountsTransfer,
    secretsPut,
    tasksSchedule,
    operationsGet,
    operationsList,
  ];
  return new Map(entries.map((m) => [m.name, m]));
}
