/**
 * Fault injection plan — the independent test oracle.
 *
 * Expected behaviors in the demo and tests are derived from THIS plan, not from
 * the engine's own output: a test that wants to assert "pricing timed out and
 * was cancelled" declares `pricing:getQuote -> timeout` here, and then checks
 * the engine independently classified the node exactly that way.
 */
import { computationFailed } from '../../kernel/errors.js';

export type FaultType = 'fail' | 'timeout' | 'delay';

export interface FaultDirective {
  type: FaultType;
  /** 1-based invocation number this directive applies to (default: every one). */
  onAttempt?: number;
  /** For `delay` / simulated `timeout`: how long the source would otherwise work. */
  delayMs?: number;
  reason?: string;
  message?: string;
}

export class FaultPlan {
  private readonly directives = new Map<string, FaultDirective[]>();

  add(source: string, method: string, directive: FaultDirective): this {
    const key = `${source}:${method}`;
    const list = this.directives.get(key) ?? [];
    list.push(directive);
    this.directives.set(key, list);
    return this;
  }

  private match(key: string, attempt: number): FaultDirective | undefined {
    return (this.directives.get(key) ?? []).find((d) => d.onAttempt === undefined || d.onAttempt === attempt);
  }

  /**
   * Evaluate at call start. Returns:
   *  - `error`   – throw immediately (injected failure);
   *  - `timeout` – cooperatively wait delayMs then time out (the propagated
   *                deadline normally aborts the wait first);
   *  - `delay`   – wait delayMs, then answer normally.
   */
  evaluate(
    source: string,
    method: string,
    attempt: number,
  ): { kind: FaultType; delayMs?: number; error?: Error } {
    const directive = this.match(`${source}:${method}`, attempt);
    if (!directive) return { kind: 'delay' };
    if (directive.type === 'fail') {
      return {
        kind: 'fail',
        error: computationFailed(
          directive.reason ?? 'INJECTED_SOURCE_FAILURE',
          directive.message ?? `injected failure for ${source}:${method}`,
          { retryable: false },
        ),
      };
    }
    return { kind: directive.type, delayMs: directive.delayMs ?? 5000 };
  }
}
