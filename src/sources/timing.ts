import type { SourceCallRecord } from './fixtureSource.js';
import type { FixtureBehavior } from './fixtureSource.js';

/**
 * Shared cooperative timing/behavior for local synthetic sources.
 * All simulated latency is abortable: when the propagated deadline fires,
 * `delay` rejects promptly instead of letting background work run unbounded.
 */
export interface ResolvedBehavior {
  latencyMs: number;
  jitterMs: number;
  hangForever: boolean;
  failWhen: NonNullable<FixtureBehavior['failWhen']>;
  supportsSnapshot: boolean;
  dataVersion: string;
  healthy: boolean;
}

export function cooperativeBehavior(behavior: FixtureBehavior): ResolvedBehavior {
  return {
    latencyMs: behavior.latencyMs ?? 0,
    jitterMs: behavior.jitterMs ?? 0,
    hangForever: behavior.hangForever ?? false,
    failWhen: behavior.failWhen ?? [],
    supportsSnapshot: behavior.supportsSnapshot ?? true,
    dataVersion: behavior.dataVersion ?? 'v1',
    healthy: behavior.healthy ?? true,
  };
}

export function resolveLatencyMs(b: ResolvedBehavior): number {
  if (b.jitterMs === 0) return b.latencyMs;
  return b.latencyMs + Math.floor(Math.random() * (b.jitterMs + 1));
}

export async function delay(
  b: ResolvedBehavior,
  signal: AbortSignal,
  call: SourceCallRecord,
): Promise<void> {
  // Record cancellation synchronously when the propagated signal fires,
  // even if the source's own await has not woken up yet. This lets tests
  // observe abort at the exact deadline rather than at the next sleep tick.
  if (signal.aborted) {
    call.aborted = true;
  } else {
    signal.addEventListener(
      'abort',
      () => {
        call.aborted = true;
        call.outcome = 'aborted';
      },
      { once: true },
    );
  }

  if (b.hangForever) {
    await waitForAbort(signal);
    call.aborted = true;
    call.outcome = 'aborted';
    throw signal.reason instanceof Error ? signal.reason : new Error('aborted');
  }
  const ms = resolveLatencyMs(b);
  await cooperativeSleep(ms, signal, () => {
    call.postAbortTicks += 1;
  });
}

/** Sleeps for `ms`, but rejects as soon as the signal aborts. */
export async function cooperativeSleep(
  ms: number,
  signal: AbortSignal,
  onPostAbortTick: () => void,
): Promise<void> {
  const stepMs = 5;
  let elapsed = 0;
  while (elapsed < ms) {
    if (signal.aborted) {
      onPostAbortTick();
      throw signal.reason instanceof Error ? signal.reason : new Error('aborted');
    }
    await new Promise((resolve) => setTimeout(resolve, Math.min(stepMs, ms - elapsed)));
    elapsed += stepMs;
  }
}

/** Resolves only on abort — proves the kernel does not wait forever. */
export async function waitForAbort(signal: AbortSignal): Promise<void> {
  if (signal.aborted) return;
  await new Promise<void>((resolve) => {
    signal.addEventListener('abort', () => resolve(), { once: true });
  });
}
