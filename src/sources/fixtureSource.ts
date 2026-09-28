import type { DataSource, SourceRequest, SourceResult } from '../types.js';
import { DomainError } from '../errors.js';
import { cooperativeBehavior, delay } from './timing.js';

/**
 * Instrumentation record for one source invocation. Tests assert on these
 * (call counts, the exact snapshot token/deadline delivered, cancellation,
 * and the absence of post-abort background work).
 */
export interface SourceCallRecord {
  seq: number;
  startedAt: number;
  finishedAt?: number;
  params: Record<string, unknown>;
  snapshotToken: string | null;
  deadline: number;
  /** AbortSignal had fired before the source produced its value. */
  aborted: boolean;
  /**
   * Ticks of simulated work observed AFTER the abort signal fired. A
   * cooperative source keeps this at 0: a timeout must not leave unbounded
   * background work running.
   */
  postAbortTicks: number;
  outcome?: 'resolved' | 'threw' | 'aborted';
}

export interface FixtureBehavior {
  /** Artificial latency before reading data; aborted early on signal. */
  latencyMs?: number;
  /** Latency jitter so diamond scheduling is realistic but non-deterministic. */
  jitterMs?: number;
  /** Ignore the deadline until aborted (used to prove kernel-side timeout). */
  hangForever?: boolean;
  /** Values of the given param that trigger a deterministic source error. */
  failWhen?: Array<{ param: string; equals: unknown; code?: string }>;
  supportsSnapshot?: boolean;
  dataVersion?: string;
  /** When false the source throws a malformed-payload computation error. */
  healthy?: boolean;
}

export interface FixtureTable {
  /** Column to match the lookup param against. */
  lookupParam: string;
  rows: Array<Record<string, unknown>>;
  /** Return the matched row set keyed under this property (list lookups). */
  listAs?: string;
}

export interface FixtureSourceOptions {
  behavior?: FixtureBehavior;
  table: FixtureTable;
  clock?: () => number;
}

/**
 * A data source backed entirely by an in-memory synthetic table. No network:
 * latency is simulated cooperatively with the propagated AbortSignal, so a
 * deadline genuinely stops the work rather than merely discarding its result.
 */
export class FixtureSource implements DataSource {
  readonly calls: SourceCallRecord[] = [];
  private seq = 0;
  private readonly behavior: ReturnType<typeof cooperativeBehavior>;
  private readonly clock: () => number;

  constructor(
    readonly name: string,
    private readonly options: FixtureSourceOptions,
  ) {
    this.behavior = cooperativeBehavior(options.behavior ?? {});
    this.clock = options.clock ?? (() => Date.now());
  }

  get invocationCount(): number {
    return this.calls.length;
  }

  async fetch(req: SourceRequest): Promise<SourceResult> {
    const call: SourceCallRecord = {
      seq: this.seq++,
      startedAt: this.clock(),
      params: { ...req.params },
      snapshotToken: req.snapshotToken,
      deadline: req.deadline,
      aborted: false,
      postAbortTicks: 0,
    };
    this.calls.push(call);

    try {
      await delay(this.behavior, req.signal, call);

      for (const rule of this.behavior.failWhen) {
        if (call.params[rule.param] === rule.equals) {
          call.outcome = 'threw';
          throw new DomainError({
            category: 'SOURCE_FAILURE',
            code: rule.code ?? 'UPSTREAM_EMPTY',
            message: `fixture source ${this.name}: deterministic failure for ${rule.param}=${String(rule.equals)}`,
            httpStatus: 502,
            retryable: false,
            details: { source: this.name, param: rule.param, value: rule.equals },
          });
        }
      }

      if (!this.behavior.healthy) {
        call.outcome = 'threw';
        throw new DomainError({
          category: 'COMPUTATION_FAILED',
          code: 'BAD_PAYLOAD',
          message: `fixture source ${this.name}: returned malformed payload`,
          httpStatus: 502,
        });
      }

      const record = this.lookup(call.params);
      call.outcome = 'resolved';
      return {
        record,
        meta: {
          supportsSnapshot: this.behavior.supportsSnapshot,
          dataVersion: this.behavior.dataVersion,
        },
      };
    } finally {
      call.finishedAt = this.clock();
      call.aborted = call.aborted || req.signal.aborted;
    }
  }

  private lookup(params: Record<string, unknown>): Record<string, unknown> {
    const { lookupParam, rows, listAs } = this.options.table;
    const key = params[lookupParam];
    if (listAs) {
      const matched = rows.filter((row) => row[lookupParam] === key);
      return { [listAs]: matched };
    }
    const row = rows.find((r) => r[lookupParam] === key);
    if (!row) {
      throw new DomainError({
        category: 'SOURCE_FAILURE',
        code: 'NOT_FOUND_IN_FIXTURE',
        message: `${this.name}: no fixture row with ${lookupParam}=${String(key)}`,
        httpStatus: 502,
        retryable: false,
      });
    }
    return { ...row };
  }
}

export type { FixtureBehavior as Behavior };
