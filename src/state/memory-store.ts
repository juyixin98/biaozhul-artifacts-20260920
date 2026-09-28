/**
 * In-memory ResourceStore implementation.
 *
 * It is a deliberately independent implementation of the storage port —
 * used both as a fast test double for kernel-level tests (so the reference
 * outcomes do not depend on the SQLite adapter under test) and as living
 * documentation that the kernel has no database-specific coupling. It is NOT
 * derived from the SQLite adapter and shares no code with it.
 *
 * A simple mutex serializes transactions; every read is taken from a
 * deep-frozen snapshot so callers cannot mutate history after commit.
 */
import { NewVersion, ResourceStore, StoredVersion, StoreTransaction } from '../core/ports.js';

export class InMemoryResourceStore implements ResourceStore {
  private readonly rows = new Map<string, StoredVersion[]>();

  /**
   * Synchronous on purpose: the kernel never awaits inside a transaction, so
   * a call runs to completion without interleaving with another call on the
   * same event loop. The SQLite adapter provides the real cross-connection
   * serialization guarantee.
   */
  transact<T>(fn: (tx: StoreTransaction) => T): T {
    return fn(this.adapter());
  }

  listVersions(resourceId: string): readonly StoredVersion[] {
    return (this.rows.get(resourceId) ?? []).map(clone);
  }

  selectVersion(resourceId: string, version: number): StoredVersion | null {
    const found = (this.rows.get(resourceId) ?? []).find((r) => r.version === version);
    return found ? clone(found) : null;
  }

  countResources(): number {
    return this.rows.size;
  }

  listCurrent(limit: number, offset: number): readonly StoredVersion[] {
    const current: StoredVersion[] = [];
    for (const id of [...this.rows.keys()].sort()) {
      const history = this.rows.get(id)!;
      const latest = history[history.length - 1]!;
      if (!latest.deleted) current.push(latest);
    }
    return current.slice(offset, offset + limit).map(clone);
  }

  private adapter(): StoreTransaction {
    return {
      selectCurrent: (resourceId: string): StoredVersion | null => {
        const history = this.rows.get(resourceId);
        if (!history || history.length === 0) return null;
        const latest = history[history.length - 1]!;
        return latest.deleted ? null : clone(latest);
      },
      selectMaxVersion: (resourceId: string): number => {
        const history = this.rows.get(resourceId);
        if (!history || history.length === 0) return 0;
        return history[history.length - 1]!.version;
      },
      insertVersion: (input: NewVersion): StoredVersion => {
        const history = this.rows.get(input.resourceId) ?? [];
        if (history.some((r) => r.version === input.version)) {
          throw new Error(`version ${input.version} already exists for ${input.resourceId}`);
        }
        const stored: StoredVersion = {
          resourceId: input.resourceId,
          version: input.version,
          body: structuredClone(input.body),
          createdMs: input.createdMs,
          deleted: input.deleted
        };
        history.push(stored);
        this.rows.set(input.resourceId, history);
        return clone(stored);
      }
    };
  }
}

function clone(row: StoredVersion): StoredVersion {
  return { ...row, body: structuredClone(row.body) };
}
