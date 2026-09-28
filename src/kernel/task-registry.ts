/**
 * Process-local registry of detached (fire-and-forget) tasks.
 *
 * Detached tasks are the "asynchronous execution" feature: the RPC returns
 * immediately with an operationId while work continues. Durability of the
 * OUTCOME lives in the StateStore (operations table); this registry only
 * tracks liveness so a task can be cancelled and so the process can wait for
 * in-flight work during graceful shutdown. Restart does not resume process
 * memory — an operation left `pending` by a crash is observable as such via
 * the query API (an honest "unknown / interrupted" state), not silently lost.
 */

export interface RegisteredTask {
  operationId: string;
  startedAt: number;
  cancel(reason: string): void;
  /** Settles when the work settles OR cancellation finishes propagating. */
  done: Promise<void>;
}

export class TaskRegistry {
  private tasks = new Map<string, RegisteredTask>();

  register(
    operationId: string,
    work: Promise<unknown>,
    controller: AbortController,
    startedAt: number,
  ): RegisteredTask {
    const task: RegisteredTask = {
      operationId,
      startedAt,
      cancel: (reason: string) => controller.abort(new Error(reason)),
      done: work.then(
        () => undefined,
        () => undefined,
      ),
    };
    this.tasks.set(operationId, task);
    void task.done.finally(() => {
      // Only delete if still the same registration.
      if (this.tasks.get(operationId) === task) this.tasks.delete(operationId);
    });
    return task;
  }

  get(operationId: string): RegisteredTask | undefined {
    return this.tasks.get(operationId);
  }

  isLive(operationId: string): boolean {
    return this.tasks.has(operationId);
  }

  liveCount(): number {
    return this.tasks.size;
  }

  /** Resolve once all currently-registered tasks settle, or timeout elapses. */
  async drain(timeoutMs: number): Promise<void> {
    const pending = [...this.tasks.values()].map((t) => t.done);
    if (pending.length === 0) return;
    let timer: NodeJS.Timeout | undefined;
    const timeout = new Promise<void>((resolve) => {
      timer = setTimeout(resolve, timeoutMs);
    });
    await Promise.race([Promise.allSettled(pending).then(() => undefined), timeout]);
    if (timer) clearTimeout(timer);
  }
}
