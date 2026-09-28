import { DatabaseSync, type SQLInputValue } from 'node:sqlite';
import type { DataSource, SourceRequest, SourceResult } from '../types.js';
import { DomainError } from '../errors.js';
import type { FixtureBehavior, SourceCallRecord } from './fixtureSource.js';
import { cooperativeBehavior, delay } from './timing.js';

export interface SqliteSourceOptions {
  db: DatabaseSync;
  /** Prepared SELECT; named params bind from resolved SourceRequest.params. */
  sql: string;
  /** Map request param name -> SQL bind name ("$id"); defaults 1:1. */
  bind?: Record<string, string>;
  behavior?: FixtureBehavior;
  /** Single row lookup throws SOURCE_FAILURE when absent; 'many' never throws. */
  cardinality?: 'one' | 'many';
  manyAs?: string;
  clock?: () => number;
}

/**
 * Data source over a local SQLite database (synthetic fixtures seeded by the
 * application). The synchronous statement run is wrapped by the same
 * cooperative deadline behaviour as FixtureSource, so cancellation semantics
 * are uniform across all local participants.
 */
export class SqliteSource implements DataSource {
  readonly calls: SourceCallRecord[] = [];
  private seq = 0;
  private readonly stmt;
  private readonly behavior: ReturnType<typeof cooperativeBehavior>;
  private readonly clock: () => number;

  constructor(
    readonly name: string,
    private readonly options: SqliteSourceOptions,
  ) {
    this.stmt = options.db.prepare(options.sql);
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
        if (req.params[rule.param] === rule.equals) {
          call.outcome = 'threw';
          throw new DomainError({
            category: 'SOURCE_FAILURE',
            code: rule.code ?? 'UPSTREAM_EMPTY',
            message: `sqlite source ${this.name}: deterministic failure for ${rule.param}=${String(rule.equals)}`,
            httpStatus: 502,
            retryable: false,
          });
        }
      }

      const binds: Record<string, unknown> = {};
      for (const [param, value] of Object.entries(req.params)) {
        const bindName = this.options.bind?.[param] ?? `$${param}`;
        binds[bindName] = value;
      }

      const cardinality = this.options.cardinality ?? 'one';
      const rows = this.stmt.all(binds as Record<string, SQLInputValue>);

      if (cardinality === 'one') {
        const row = rows[0];
        if (!row) {
          call.outcome = 'threw';
          throw new DomainError({
            category: 'SOURCE_FAILURE',
            code: 'NOT_FOUND_IN_FIXTURE',
            message: `sqlite source ${this.name}: no row for ${JSON.stringify(req.params)}`,
            httpStatus: 502,
            retryable: false,
          });
        }
        call.outcome = 'resolved';
        return {
          record: row,
          meta: {
            supportsSnapshot: this.behavior.supportsSnapshot,
            dataVersion: this.behavior.dataVersion,
          },
        };
      }

      call.outcome = 'resolved';
      return {
        record: { [this.options.manyAs ?? 'items']: rows },
        meta: {
          supportsSnapshot: this.behavior.supportsSnapshot,
          dataVersion: this.behavior.dataVersion,
        },
      };
    } finally {
      call.finishedAt = this.clock();
      call.aborted = req.signal.aborted;
    }
  }
}
