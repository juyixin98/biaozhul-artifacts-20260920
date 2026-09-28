/**
 * Execution kernel.
 *
 * Each mutating command runs the whole sequence — select current
 * representation, evaluate preconditions, apply the change, read back the
 * committed row — inside a single store transaction. The ETag and body placed
 * in the HTTP response are derived from that one read-back snapshot, never
 * from pre-insert state.
 *
 * The kernel is transport-agnostic and storage-agnostic: Fastify lives in the
 * adapter layer, persistence behind the ResourceStore port.
 */
import { evaluatePreconditions, HttpMethod, RawConditionHeaders } from '../contract/conditions.js';
import { DomainError, NotFoundError, NotModifiedError, TraceStep } from '../contract/errors.js';
import { formatETag } from '../contract/etag.js';
import { applyMergePatch } from './merge-patch.js';
import { etagFor } from './versioning.js';
import { NewVersion, ResourceStore, StoredVersion, StoreTransaction } from './ports.js';

export interface Clock {
  now(): number;
}

export interface Representation {
  readonly snapshot: StoredVersion;
  readonly etagHeader: string;
}

export type ReadOutcome =
  | { readonly kind: 'found'; readonly representation: Representation; readonly trace: readonly TraceStep[] }
  | { readonly kind: 'not-modified'; readonly representation: Representation; readonly trace: readonly TraceStep[] }
  | null;

export type WriteStatus = 200 | 201 | 204;

export interface WriteOutcome {
  readonly status: WriteStatus;
  /** Present for 200/201; null for 204 (DELETE), which carries no representation. */
  readonly representation: Representation | null;
  readonly trace: readonly TraceStep[];
}

function toRepresentation(snapshot: StoredVersion): Representation {
  const tag = etagFor(snapshot.version, snapshot.body);
  return { snapshot, etagHeader: formatETag(tag.opaque, tag.weak) };
}

export class ResourceKernel {
  constructor(
    private readonly store: ResourceStore,
    private readonly clock: Clock = { now: () => Date.now() }
  ) {}

  /** Conditional read; null when the resource does not exist. */
  get(id: string, headers: RawConditionHeaders): ReadOutcome {
    return this.store.transact((tx) => {
      const current = tx.selectCurrent(id);
      if (!current) return null;
      const representation = toRepresentation(current);
      try {
        const { trace } = evaluatePreconditions(
          'GET',
          headers,
          {
            kind: 'present',
            representation: {
              etag: etagFor(current.version, current.body),
              lastModifiedMs: current.createdMs
            }
          }
        );
        return { kind: 'found', representation, trace };
      } catch (err) {
        if (err instanceof NotModifiedError) {
          // 304 carries validator headers from the same committed snapshot.
          return { kind: 'not-modified', representation, trace: err.trace };
        }
        throw err;
      }
    });
  }

  /**
   * PUT: create when absent (201) or replace when present (200). Conditions
   * are evaluated against the current state before the write, in this tx.
   */
  put(id: string, body: unknown, headers: RawConditionHeaders): WriteOutcome {
    return this.mutate('PUT', id, headers, (tx, current) => {
      // Version numbering continues across tombstones so a re-created
      // resource never collides with a historical row.
      const nextVersion = tx.selectMaxVersion(id) + 1;
      const row: NewVersion = {
        resourceId: id,
        version: nextVersion,
        body,
        createdMs: this.clock.now(),
        deleted: false
      };
      return { status: current ? 200 : 201, inserted: tx.insertVersion(row) };
    });
  }

  /** PATCH (JSON Merge Patch, RFC 7386): the resource must exist. */
  patch(id: string, patch: unknown, headers: RawConditionHeaders): WriteOutcome {
    return this.mutate('PATCH', id, headers, (tx, current) => {
      if (!current) throw new NotFoundError(`Cannot PATCH absent resource "${id}"`);
      const row: NewVersion = {
        resourceId: id,
        version: current.version + 1,
        body: applyMergePatch(current.body, patch),
        createdMs: this.clock.now(),
        deleted: false
      };
      return { status: 200, inserted: tx.insertVersion(row) };
    });
  }

  /** DELETE: append a tombstone (204). Deleting an absent resource is 404. */
  remove(id: string, headers: RawConditionHeaders): WriteOutcome {
    return this.mutate('DELETE', id, headers, (tx, current) => {
      if (!current) throw new NotFoundError(`Resource "${id}" does not exist`);
      const row: NewVersion = {
        resourceId: id,
        version: current.version + 1,
        body: null,
        createdMs: this.clock.now(),
        deleted: true
      };
      tx.insertVersion(row);
      return { status: 204, inserted: null };
    });
  }

  /**
   * Shared transactional body for writes: select → evaluate → apply →
   * read back. All contract errors carry the trace accumulated so far.
   */
  private mutate(
    method: HttpMethod,
    id: string,
    headers: RawConditionHeaders,
    apply: (
      tx: StoreTransaction,
      current: StoredVersion | null
    ) => { status: WriteStatus; inserted: StoredVersion | null }
  ): WriteOutcome {
    return this.store.transact((tx) => {
      const current = tx.selectCurrent(id);
      const selection = current
        ? {
            kind: 'present' as const,
            representation: {
              etag: etagFor(current.version, current.body),
              lastModifiedMs: current.createdMs
            }
          }
        : { kind: 'absent' as const };

      const { trace } = evaluatePreconditions(method, headers, selection);
      const { status, inserted } = apply(tx, current);

      // Read-back: the response representation is the committed snapshot,
      // so ETag and body provably come from the same version.
      const representation = inserted ? toRepresentation(inserted) : null;
      return { status, representation, trace };
    });
  }
}

/** Re-throw contract/domain errors unchanged; wrap anything else explicitly. */
export function asDomainError(err: unknown): DomainError {
  if (err instanceof DomainError) return err;
  const message = err instanceof Error ? err.message : String(err);
  return new DomainError('INTERNAL_ERROR', 500, `Unexpected kernel failure: ${message}`);
}
