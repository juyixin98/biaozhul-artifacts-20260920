/**
 * Source registry — sources register under a name the contract references.
 *
 * The registry also records every invocation (count + last context), which the
 * test suite uses to assert call counts and that the same snapshot token and
 * deadline reached every source.
 */
import type { Source, CallContext } from './types.js';

export interface InvocationRecord {
  source: string;
  method: string;
  request: unknown;
  contextSnapshot: {
    snapshotToken: string;
    deadline: number;
    timeoutMs: number;
    runId: string;
  };
  at: number;
}

export class SourceRegistry {
  private readonly sources = new Map<string, Source>();
  private readonly invocations: InvocationRecord[] = [];

  register(source: Source): void {
    if (this.sources.has(source.name)) {
      throw new Error(`source already registered: ${source.name}`);
    }
    this.sources.set(source.name, source);
  }

  get(name: string): Source {
    const source = this.sources.get(name);
    if (!source) {
      throw new Error(`unknown source: ${name}`);
    }
    return source;
  }

  has(name: string): boolean {
    return this.sources.has(name);
  }

  record(source: string, method: string, request: unknown, ctx: CallContext): void {
    this.invocations.push({
      source,
      method,
      request,
      contextSnapshot: {
        snapshotToken: ctx.snapshotToken,
        deadline: ctx.deadline,
        timeoutMs: ctx.timeoutMs,
        runId: ctx.runId,
      },
      at: Date.now(),
    });
  }

  count(source?: string, method?: string): number {
    return this.invocations.filter(
      (i) =>
        (source === undefined || i.source === source) &&
        (method === undefined || i.method === method),
    ).length;
  }

  all(): readonly InvocationRecord[] {
    return this.invocations;
  }
}
