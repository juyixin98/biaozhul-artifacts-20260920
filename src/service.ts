/**
 * Service orchestration: binds a patch request to an expected document
 * version, runs the pure kernel against the loaded state inside a single
 * immediate SQLite transaction, and records a fully explainable audit trail.
 *
 * Concurrency/atomicity model:
 *   BEGIN IMMEDIATE -> row lock
 *     -> load (id, doc, version)
 *     -> compare version to expectedVersion (VERSION_CONFLICT otherwise)
 *     -> applyPatch (pure; never mutates its input)
 *     -> on success: UPDATE ... WHERE version = current (guarded CAS)
 *   COMMIT | ROLLBACK
 * Audit insert happens afterwards in a separate transaction, so a rollback
 * still leaves diagnostics behind without changing document state.
 */

import { randomUUID } from 'node:crypto';
import { PatchError } from './errors';
import type { JsonValue } from './equality';
import { applyPatch, type PatchOutcome, type StepTrace } from './patch';
import { DocumentStore } from './store';
import type { Logger } from './logger';

export interface PatchRequest {
  readonly docId: string;
  readonly ops: unknown;
  readonly expectedVersion: number | null;
  readonly requestId?: string;
}

export interface PatchResponse {
  readonly requestId: string;
  readonly docId: string;
  readonly ok: boolean;
  readonly fromVersion: number;
  readonly toVersion: number | null;
  readonly applied: number;
  readonly result: JsonValue;
  readonly failure: {
    readonly category: string;
    readonly failedAtIndex: number;
    readonly message: string;
    readonly details: Record<string, unknown>;
  } | null;
  readonly steps: readonly StepTrace[];
}

export class PatchService {
  constructor(
    private readonly store: DocumentStore,
    private readonly logger: Logger,
  ) {}

  createDocument(id: string, doc: JsonValue): { id: string; version: number; doc: JsonValue } {
    const validated = assertJsonDoc(doc);
    return this.store.createDocument(id, validated);
  }

  getDocument(id: string) {
    return this.store.getDocument(id);
  }

  listDocuments(): Array<{ id: string; version: number }> {
    return this.store.listDocuments();
  }

  apply(request: PatchRequest): PatchResponse {
    const requestId = request.requestId ?? randomUUID();
    const log = this.logger.child({ requestId, docId: request.docId });
    log.log({
      level: 'info',
      event: 'patch.received',
      expectedVersion: request.expectedVersion,
      opCount: Array.isArray(request.ops) ? request.ops.length : null,
    });

    let outcome: PatchOutcome | undefined;
    let loadedVersion: number | undefined;
    let loadedDoc: JsonValue | undefined;

    try {
      const result = this.store.withImmediateTransaction((tx) => {
        const row = tx.loadForUpdate(request.docId);
        loadedVersion = row.version;
        loadedDoc = row.doc;
        log.log({
          level: 'debug',
          event: 'patch.loaded',
          version: row.version,
        });

        if (request.expectedVersion !== null && request.expectedVersion !== undefined) {
          if (!Number.isInteger(request.expectedVersion) || request.expectedVersion < 0) {
            throw new PatchError(
              'BAD_REQUEST_BODY',
              'expectedVersion must be a non-negative integer',
              null,
              { received: request.expectedVersion },
            );
          }
          if (row.version !== request.expectedVersion) {
            throw new PatchError(
              'VERSION_CONFLICT',
              'Document version does not match expectedVersion; patch rejected without execution',
              null,
              { expectedVersion: request.expectedVersion, actualVersion: row.version },
            );
          }
        }

        outcome = applyPatch(row.doc, request.ops);
        if (!outcome.ok) {
          // Kernel failure: throw to force ROLLBACK. Nothing was written.
          throw new PatchError(
            outcome.category as PatchError['category'],
            outcome.message,
            outcome.failedAtIndex,
            { ...outcome.details, kernel: true },
          );
        }

        // An empty patch is a successful no-op: it must not mint a new version.
        if (outcome.applied === 0) {
          log.log({ level: 'info', event: 'patch.noop', version: row.version });
          return { nextVersion: row.version, fromVersion: row.version, changed: false };
        }

        const nextVersion = row.version + 1;
        tx.storeUpdated(row.id, outcome.result, nextVersion);
        log.log({
          level: 'info',
          event: 'patch.applied',
          fromVersion: row.version,
          toVersion: nextVersion,
          applied: outcome.applied,
        });
        return { nextVersion, fromVersion: row.version, changed: true };
      });

      const success = outcome;
      if (!success || !success.ok) throw new Error('unreachable: successful transaction with failed outcome');

      this.store.recordAudit({
        requestId,
        docId: request.docId,
        expectedVersion: request.expectedVersion ?? null,
        ok: true,
        category: null,
        failedAtIndex: null,
        message: null,
        details: {},
        fromVersion: result.fromVersion,
        toVersion: result.nextVersion,
        applied: success.applied,
        traces: success.traces,
      });

      return {
        requestId,
        docId: request.docId,
        ok: true,
        fromVersion: result.fromVersion,
        toVersion: result.nextVersion,
        applied: success.applied,
        result: success.result,
        failure: null,
        steps: success.traces,
      };
    } catch (err) {
      return this.handleFailure(err, request, requestId, loadedVersion, loadedDoc, outcome, log);
    }
  }

  private handleFailure(
    err: unknown,
    request: PatchRequest,
    requestId: string,
    fromVersion: number | undefined,
    loadedDoc: JsonValue | undefined,
    kernelOutcome: PatchOutcome | undefined,
    log: ReturnType<Logger['child']>,
  ): PatchResponse {
    const pe =
      err instanceof PatchError
        ? err
        : new PatchError('INTERNAL_ERROR', err instanceof Error ? err.message : String(err));

    // When the failure originated inside the kernel, its outcome carries the
    // per-step traces. Pre-kernel failures (version/not-found) have no steps.
    const fromKernel = pe.details.kernel === true;
    const steps: StepTrace[] =
      fromKernel && kernelOutcome && !kernelOutcome.ok ? [...kernelOutcome.traces] : [];
    const details = fromKernel ? stripKernelFlag(pe.details) : pe.details;

    const versionForAudit = pe.category === 'DOCUMENT_NOT_FOUND' ? null : (fromVersion ?? null);

    log.log({
      level: pe.category === 'VERSION_CONFLICT' || pe.category === 'TEST_FAILURE' ? 'warn' : 'error',
      event: 'patch.rejected',
      category: pe.category,
      failedAtIndex: pe.opIndex,
      fromVersion: versionForAudit,
      message: pe.message,
      details,
    });

    this.store.recordAudit({
      requestId,
      docId: request.docId,
      expectedVersion: request.expectedVersion ?? null,
      ok: false,
      category: pe.category,
      failedAtIndex: pe.opIndex,
      message: pe.message,
      details,
      fromVersion: versionForAudit,
      toVersion: null,
      applied: null,
      traces: steps,
    });

    return {
      requestId,
      docId: request.docId,
      ok: false,
      fromVersion: fromVersion ?? -1,
      toVersion: null,
      applied: 0,
      result: loadedDoc ?? null,
      failure: {
        category: pe.category,
        failedAtIndex: pe.opIndex ?? -1,
        message: pe.message,
        details,
      },
      steps,
    };
  }
}

function stripKernelFlag(details: Record<string, unknown>): Record<string, unknown> {
  const { kernel: _kernel, ...rest } = details;
  return rest;
}

function assertJsonDoc(doc: unknown): JsonValue {
  if (doc === null) return null;
  if (
    typeof doc === 'object' ||
    typeof doc === 'string' ||
    typeof doc === 'boolean' ||
    typeof doc === 'number'
  ) {
    return doc as JsonValue;
  }
  throw new PatchError('BAD_REQUEST_BODY', 'Document body must be valid JSON', null, {
    receivedType: typeof doc,
  });
}
