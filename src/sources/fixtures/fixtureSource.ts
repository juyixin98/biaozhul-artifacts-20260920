/**
 * FixtureSource — local, deterministic base for every synthetic source.
 *
 * It never touches the network. Subclasses (or a `methods` map) provide the
 * business lookup; this base enforces the cross-cutting source contract:
 *  - cooperative cancellation via ctx.signal,
 *  - never working past ctx.deadline (the wait is bounded by remaining budget),
 *  - fault injection driven by the independent FaultPlan.
 */
import type { CallContext, Source, SourceCapabilities } from '../types.js';
import type { FaultPlan } from './faults.js';
import { cancellableDelay } from '../../kernel/timing.js';
import { resourceExhausted } from '../../kernel/errors.js';

export type MethodHandler<TRequest = unknown> = (
  request: TRequest,
  ctx: CallContext,
) => unknown | Promise<unknown>;

export interface FixtureOptions {
  faultPlan?: FaultPlan;
  /** Extra per-op latency simulation in ms (bounded by the deadline). */
  baseLatencyMs?: number;
}

export class FixtureSource implements Source {
  private readonly handlers = new Map<string, MethodHandler>();
  consistency?: (ctx: CallContext) => { snapshotHonored: boolean; dataVersion: string };

  constructor(
    readonly name: string,
    readonly capabilities: SourceCapabilities,
    private readonly options: FixtureOptions = {},
  ) {}

  method<TRequest = unknown>(name: string, handler: MethodHandler<TRequest>): this {
    this.handlers.set(name, handler as MethodHandler);
    return this;
  }

  /** Declare how this source reports snapshot/version for each call. */
  reportConsistency(fn: (ctx: CallContext) => { snapshotHonored: boolean; dataVersion: string }): this {
    this.consistency = fn;
    return this;
  }

  async call(method: string, request: unknown, ctx: CallContext): Promise<unknown> {
    if (ctx.signal.aborted) {
      throw resourceExhausted('CANCELLED', `${this.name}.${method} called after abort`);
    }
    const handler = this.handlers.get(method);
    if (!handler) {
      throw new Error(`source ${this.name} has no method ${method}`);
    }

    const fault = this.options.faultPlan?.evaluate(this.name, method, 1);
    if (fault?.kind === 'fail' && fault.error) {
      throw fault.error;
    }
    const wanted = (this.options.baseLatencyMs ?? 0) + (fault?.delayMs ?? 0);
    if (wanted > 0) {
      // Wait the full intended duration COOPERATIVELY: when the duration
      // exceeds the propagated deadline, ctx.signal aborts this wait at the
      // deadline and the rejection (DEADLINE_EXCEEDED) propagates. No timer
      // outlives the abort, so no background work remains.
      await cancellableDelay(wanted, ctx.signal);
    }

    return handler(request, ctx);
  }
}
