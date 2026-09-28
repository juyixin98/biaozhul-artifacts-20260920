/**
 * HTTP interface (Fastify).
 *
 * Two concerns live here:
 *   - mutation:   POST /documents/:id/patch  (contract -> kernel -> store)
 *   - diagnostics: GET /documents/:id, /documents/:id/events, /events/:requestId
 *
 * Every response and every log line carries the request identity. Failure
 * responses expose the typed category, the failing operation index, and the
 * steps already applied; they never claim success for an uncertain result.
 */

import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';

import { ContractError, parsePatch, type PatchOperation } from './contract.js';
import { JsonLogger } from './logger.js';
import { DocumentStore, StoreError } from './store.js';

interface PatchRequestBody {
  expectedVersion?: unknown;
  patch?: unknown;
  requestId?: unknown;
}

interface DocParams {
  id: string;
}

interface EventParams {
  requestId: string;
}

export interface ApiDeps {
  store: DocumentStore;
  logger: JsonLogger;
}

const HTTP_STATUS: Record<string, number> = {
  MALFORMED_JSON: 400,
  PATCH_NOT_ARRAY: 400,
  OP_NOT_OBJECT: 400,
  UNKNOWN_OP: 400,
  MISSING_FIELD: 400,
  BAD_FIELD_TYPE: 400,
  MALFORMED_POINTER: 400,
  MOVE_INTO_SELF: 400,
  EMPTY_PATCH: 400,
  TEST_FAILED: 422,
  POINTER_ERROR: 422,
  MOVE_TARGET_DESCENDANT: 422,
  ROOT_LOCATION_INVALID: 422,
  DOCUMENT_NOT_FOUND: 404,
  DOCUMENT_ALREADY_EXISTS: 409,
  VERSION_CONFLICT: 409,
  KERNEL_FAILURE: 422,
};

export async function registerRoutes(app: FastifyInstance, deps: ApiDeps): Promise<void> {
  const { store, logger } = deps;

  app.get('/health', async () => ({ status: 'ok' }));

  app.post('/documents', async (request: FastifyRequest, reply: FastifyReply) => {
    const requestId = resolveRequestId(request);
    const log = logger.child({ requestId, phase: 'api' });
    const body = request.body as { id?: unknown; document?: unknown } | undefined;

    if (typeof body?.id !== 'string' || body.id.length === 0) {
      return failure(reply, 400, requestId, 'MISSING_FIELD', "body must contain string 'id'");
    }
    if (body.document === undefined) {
      return failure(reply, 400, requestId, 'MISSING_FIELD', "body must contain 'document'");
    }
    try {
      const record = store.createDocument(body.id, body.document);
      log.info('document.created', {
        documentId: record.id,
        version: record.version,
        storage: 'sqlite',
      });
      return reply.code(201).send({
        requestId,
        documentId: record.id,
        document: record.document,
        version: record.version,
        updatedAt: record.updatedAt,
      });
    } catch (error) {
      if (error instanceof StoreError) {
        return sendStoreError(reply, requestId, error, log);
      }
      throw error;
    }
  });

  app.get('/documents/:id', async (request: FastifyRequest<{ Params: DocParams }>, reply) => {
    const requestId = resolveRequestId(request);
    try {
      const record = store.getDocument(request.params.id);
      return {
        requestId,
        documentId: record.id,
        document: record.document,
        version: record.version,
        updatedAt: record.updatedAt,
      };
    } catch (error) {
      if (error instanceof StoreError) {
        return sendStoreError(reply, requestId, error, logger.child({ requestId }));
      }
      throw error;
    }
  });

  app.post('/documents/:id/patch', async (request: FastifyRequest<{ Params: DocParams }>, reply) => {
    const requestId = resolveRequestId(request);
    const documentId = request.params.id;
    const log = logger.child({ requestId, documentId, phase: 'contract' });
    const body = (request.body ?? {}) as PatchRequestBody;

    if (typeof body.requestId === 'string' && body.requestId !== requestId) {
      // The header wins; mismatch is reported rather than silently switched.
      log.warn('request.id.mismatch', {
        certainty: 'uncertain',
        headerRequestId: requestId,
        bodyRequestId: body.requestId,
      });
    }

    // ---- Phase 1: contract parsing ----
    let operations: PatchOperation[];
    try {
      if (typeof body.expectedVersion !== 'number' || !Number.isInteger(body.expectedVersion) || body.expectedVersion < 0) {
        throw new ContractError(
          'MISSING_FIELD',
          "body must contain non-negative integer 'expectedVersion' the patch is bound to",
          -1,
          'expectedVersion',
        );
      }
      const parsed = parsePatch(body.patch);
      operations = parsed.operations;
      log.info('patch.received', {
        expectedVersion: body.expectedVersion,
        operationsTotal: operations.length,
        ops: operations.map((op) => op.op),
      });
    } catch (error) {
      if (error instanceof ContractError) {
        log.warn('patch.rejected', {
          outcome: 'failure',
          certainty: 'certain',
          category: error.category,
          failedAtIndex: error.operationIndex,
          field: error.field,
          reason: error.message,
        });
        return failure(reply, HTTP_STATUS[error.category] ?? 400, requestId, error.category, error.message, {
          failedAtIndex: error.operationIndex >= 0 ? error.operationIndex : undefined,
          field: error.field,
        });
      }
      throw error;
    }

    const expectedVersion = body.expectedVersion;

    // ---- Phases 2+3: kernel execution inside the version-checked transaction ----
    try {
      const applied = store.applyPatch(documentId, expectedVersion, operations, requestId);
      for (const step of applied.steps) {
        log.debug('patch.step.applied', {
          phase: 'kernel',
          stepIndex: step.index,
          op: step.op,
          path: step.path,
          ...(step.from !== undefined ? { from: step.from } : {}),
        });
      }
      log.info('patch.applied', {
        phase: 'store',
        outcome: 'success',
        certainty: 'certain',
        storage: 'sqlite',
        baseVersion: applied.baseVersion,
        newVersion: applied.newVersion,
        operationsApplied: applied.steps.length,
      });
      return reply.code(200).send({
        requestId,
        documentId,
        baseVersion: applied.baseVersion,
        newVersion: applied.newVersion,
        result: applied.document,
        steps: applied.steps.map((step) => ({
          index: step.index,
          op: step.op,
          path: step.path,
          ...(step.from !== undefined ? { from: step.from } : {}),
          status: step.status,
          resultAfter: step.resultAfter,
        })),
      });
    } catch (error) {
      if (error instanceof StoreError && error.category === 'VERSION_CONFLICT') {
        const actual = error.details?.actualVersion;
        store.recordConflict(
          requestId,
          documentId,
          expectedVersion,
          actual ?? -1,
          operations.length,
        );
        log.warn('patch.conflict', {
          phase: 'store',
          outcome: 'failure',
          certainty: 'certain',
          category: 'VERSION_CONFLICT',
          expectedVersion,
          actualVersion: actual,
          reason: error.message,
        });
        return failure(reply, 409, requestId, 'VERSION_CONFLICT', error.message, {
          expectedVersion,
          actualVersion: actual,
        });
      }
      if (error instanceof StoreError && error.category === 'KERNEL_FAILURE') {
        const kernel = error.details!.kernel!;
        log.warn('patch.rejected', {
          phase: 'kernel',
          outcome: 'failure',
          certainty: 'certain',
          category: kernel.category,
          failedAtIndex: kernel.failedAtIndex,
          stepsAppliedBeforeFailure: kernel.appliedBeforeFailure.length,
          reason: kernel.message,
        });
        return failure(reply, 422, requestId, kernel.category, kernel.message, {
          failedAtIndex: kernel.failedAtIndex,
          rolledBack: kernel.rolledBack,
          baseVersion: error.details?.expectedVersion,
          appliedBeforeFailure: kernel.appliedBeforeFailure.map((step) => ({
            index: step.index,
            op: step.op,
            path: step.path,
            ...(step.from !== undefined ? { from: step.from } : {}),
          })),
        });
      }
      if (error instanceof StoreError) {
        return sendStoreError(reply, requestId, error, log);
      }
      // Unknown failure: report it explicitly as uncertain, never as success.
      log.error('patch.error.unexpected', {
        outcome: 'failure',
        certainty: 'uncertain',
        reason: error instanceof Error ? error.message : String(error),
      });
      throw error;
    }
  });

  // ---- Diagnostics ----

  app.get(
    '/documents/:id/events',
    async (request: FastifyRequest<{ Params: DocParams; Querystring: { limit?: string } }>, reply) => {
      const requestId = resolveRequestId(request);
      try {
        const doc = store.getDocument(request.params.id);
        const limit = request.query.limit ? clampLimit(request.query.limit) : 50;
        const events = store.listEvents(doc.id, limit);
        return { requestId, documentId: doc.id, currentVersion: doc.version, events };
      } catch (error) {
        if (error instanceof StoreError) {
          return sendStoreError(reply, requestId, error, logger.child({ requestId }));
        }
        throw error;
      }
    },
  );

  app.get(
    '/events/:requestId',
    async (request: FastifyRequest<{ Params: EventParams }>, reply) => {
      const requestId = resolveRequestId(request);
      const event = store.getEvent(request.params.requestId);
      if (!event) {
        return failure(reply, 404, requestId, 'EVENT_NOT_FOUND',
          `no patch event with request id ${JSON.stringify(request.params.requestId)}`);
      }
      return { requestId, event };
    },
  );
}

function clampLimit(raw: string): number {
  const n = Number(raw);
  if (!Number.isInteger(n)) return 50;
  return Math.min(200, Math.max(1, n));
}

function resolveRequestId(request: FastifyRequest): string {
  const header = request.headers['x-request-id'];
  if (typeof header === 'string' && /^[A-Za-z0-9._:-]{1,128}$/.test(header)) {
    return header;
  }
  return request.id;
}

function failure(
  reply: FastifyReply,
  status: number,
  requestId: string,
  category: string,
  message: string,
  extra: Record<string, unknown> = {},
) {
  return reply.code(status).send({
    requestId,
    success: false,
    error: { category, message, ...extra },
  });
}

function sendStoreError(
  reply: FastifyReply,
  requestId: string,
  error: StoreError,
  log: JsonLogger,
) {
  const status = HTTP_STATUS[error.category] ?? 500;
  log.warn('store.error', {
    outcome: 'failure',
    certainty: 'certain',
    category: error.category,
    reason: error.message,
  });
  return failure(reply, status, requestId, error.category, error.message, {
    ...(error.details?.expectedVersion !== undefined
      ? { expectedVersion: error.details.expectedVersion }
      : {}),
    ...(error.details?.actualVersion !== undefined
      ? { actualVersion: error.details.actualVersion }
      : {}),
  });
}
