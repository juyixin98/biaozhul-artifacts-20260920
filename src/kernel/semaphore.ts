import { DomainError } from '../errors.js';

/**
 * Bounded semaphore with a finite wait queue and ownership handoff.
 *
 * - When all slots are busy and the wait queue is at `maxQueue`, acquire()
 *   fails fast with RESOURCE_EXHAUSTED (no unbounded queueing).
 * - A queued acquire is rejected immediately when its AbortSignal fires.
 * - release() hands the slot DIRECTLY to the next waiter (active count stays
 *   constant across the handoff), preventing a capacity window where a new
 *   arrival could jump ahead of queued waiters.
 */
export class BoundedSemaphore {
  private active = 0;
  private readonly waiters: Array<{
    nodeId: string;
    grant: () => void;
    reject: (error: Error) => void;
  }> = [];
  private readonly releaseFn: () => void;

  constructor(
    private readonly maxConcurrent: number,
    private readonly maxQueue: number,
  ) {
    if (maxConcurrent < 1) throw new Error('maxConcurrent must be >= 1');
    if (maxQueue < 0) throw new Error('maxQueue must be >= 0');
    // Bound once so every granted acquirer shares the same release identity.
    this.releaseFn = this.release.bind(this);
  }

  get activeCount(): number {
    return this.active;
  }

  get queuedCount(): number {
    return this.waiters.length;
  }

  async acquire(nodeId: string, signal?: AbortSignal): Promise<() => void> {
    if (signal?.aborted) throw this.abortReason(signal);

    if (this.active < this.maxConcurrent) {
      this.active += 1;
      return this.releaseFn;
    }
    if (this.waiters.length >= this.maxQueue) {
      throw new DomainError({
        category: 'RESOURCE_EXHAUSTED',
        code: 'LOCAL_CONCURRENCY_LIMIT',
        message: `node ${nodeId}: concurrency limit reached (${this.maxConcurrent} active, queue cap ${this.maxQueue})`,
        httpStatus: 503,
        retryable: true,
        details: { nodeId, active: this.active, queue: this.waiters.length },
      });
    }

    // Queue. Resolved only by an ownership handoff in release(); rejected on
    // abort. Either way active is unchanged (the slot still has an owner).
    await new Promise<void>((resolve, reject) => {
      const entry: { nodeId: string; grant: () => void; reject: (e: Error) => void } = {
        nodeId,
        grant: () => {},
        reject,
      };
      const onAbort = (): void => {
        const index = this.waiters.indexOf(entry);
        if (index >= 0) this.waiters.splice(index, 1);
        reject(this.abortReason(signal!));
      };
      signal?.addEventListener('abort', onAbort, { once: true });
      // Same object reference that is queued -> abort removal always matches.
      entry.grant = (): void => {
        signal?.removeEventListener('abort', onAbort);
        resolve();
      };
      this.waiters.push(entry);
    });

    return this.releaseFn;
  }

  private abortReason(signal: AbortSignal): Error {
    return signal.reason instanceof Error ? signal.reason : new Error('aborted while queued');
  }

  private release(): void {
    const next = this.waiters.shift();
    if (next) {
      // Ownership handoff: active stays constant, no capacity window.
      next.grant();
      return;
    }
    this.active -= 1;
  }
}
