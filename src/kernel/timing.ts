/**
 * Deadline / cancellation helpers.
 *
 * The engine owns ONE AbortController per run: it fires when the deadline is
 * reached OR when the run is cancelled early (a required call failed). Every
 * source receives the same signal; `cancellableDelay` guarantees no timer keeps
 * an event loop alive after abort — there is no unbounded background work.
 */
import { CompositeError, resourceExhausted } from './errors.js';

export interface DeadlineHandle {
  controller: AbortController;
  deadline: number;
  timeoutMs: number;
  startedAt: number;
  cancel: (reason?: string) => void;
  /** Clear the deadline timer (idempotent). */
  cleanup: () => void;
}

export function createDeadline(timeoutMs: number, runId: string): DeadlineHandle {
  const controller = new AbortController();
  const startedAt = Date.now();
  const deadline = startedAt + timeoutMs;
  let timer: NodeJS.Timeout | null = null;
  let settled = false;

  timer = setTimeout(() => {
    if (settled) return;
    settled = true;
    controller.abort(
      resourceExhausted('DEADLINE_EXCEEDED', `run ${runId} exceeded ${timeoutMs}ms deadline`, {
        runId,
        timeoutMs,
      }),
    );
  }, timeoutMs);
  // Don't let the deadline timer keep the process alive on its own.
  timer.unref?.();

  const cancel = (reason?: string): void => {
    if (settled) return;
    settled = true;
    if (timer !== null) clearTimeout(timer);
    controller.abort(
      resourceExhausted('RUN_CANCELLED', reason ?? `run ${runId} cancelled after terminal failure`, {
        runId,
        ...(reason !== undefined ? { reason } : {}),
      }),
    );
  };

  const cleanup = (): void => {
    if (timer !== null) clearTimeout(timer);
  };

  return { controller, deadline, timeoutMs, startedAt, cancel, cleanup };
}

/**
 * Resolve after `ms` OR reject promptly when the signal aborts.
 * Used by fixture sources to simulate latency cooperatively.
 */
export function cancellableDelay(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) {
    return Promise.reject(abortReason(signal));
  }
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(abortReason(signal));
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

/** Extract the CompositeError carried by an abort, or synthesize one. */
export function abortReason(signal: AbortSignal): CompositeError {
  const reason = (signal as AbortSignal & { reason?: unknown }).reason;
  if (reason instanceof CompositeError) return reason;
  return resourceExhausted('CANCELLED', 'operation cancelled before its deadline');
}
