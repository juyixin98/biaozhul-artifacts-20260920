/**
 * HTTP diagnostic interface (Fastify).
 *
 * Routes:
 *   POST   /documents                      create a document
 *   GET    /documents                      list documents
 *   GET    /documents/:id                  read a document + version
 *   POST   /documents/:id/patch            apply a version-bound patch
 *   GET    /diagnostics/requests/:reqId    full audit record with step traces
 *   GET    /documents/:id/history          recent audit records for a document
 *   GET    /health                         liveness
 *
 * Every response uses one envelope; every log line carries the request id
 * (client-supplied via X-Request-Id / body.requestId, or generated).
 */

import { randomUUID } from 'node:crypto';
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import Fastify from 'fastify';
import { PatchError } from './errors';
import type { JsonValue } from './equality';
import type { Logger } from './logger';
import type { PatchService } from './service';
import { DocumentStore } from './store';

declare module 'fastify' {
  interface FastifyRequest {
    reqId: string;
  }
}

export interface AppDeps {
  readonly service: PatchService;
  readonly store: DocumentStore;
  readonly logger: Logger;
}

const REQUEST_ID_PATTERN = /^[A-Za-z0-9._-]{1,128}$/;
const DOC_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;

function envelopeOk(
  reply: FastifyReply,
  requestId: string,
  data: unknown,
  extraMeta: Record<string, unknown> = {},
) {
  return reply.code(200).send({
    success: true,
    data,
    error: null,
    meta: { requestId, ...extraMeta },
  });
}

function envelopeFail(
  reply: FastifyReply,
  requestId: string,
  status: number,
  category: string,
  message: string,
  details: Record<string, unknown>,
  extraMeta: Record<string, unknown> = {},
) {
  return reply.code(status).send({
    success: false,
    data: null,
    error: { category, message, details },
    meta: { requestId, ...extraMeta },
  });
}

function headerRequestId(req: FastifyRequest): string | null {
  const header = req.headers['x-request-id'];
  if (typeof header === 'string' && REQUEST_ID_PATTERN.test(header)) return header;
  return null;
}

export function buildApp(deps: AppDeps): FastifyInstance {
  const { service, store, logger } = deps;

  const app = Fastify({
    logger: false,
    bodyLimit: 1_048_576,
  });
  app.decorateRequest('reqId', '');

  app.addHook('onRequest', (req, _reply, done) => {
    req.reqId = headerRequestId(req) ?? randomUUID();
    logger.child({ requestId: req.reqId }).log({
      level: 'info',
      event: 'http.request',
      method: req.method,
      url: req.url,
    });
    done();
  });

  // Map malformed JSON / unsupported media type to the stable envelope.
  app.setErrorHandler((err, req, reply) => {
    const rid = req.reqId || headerRequestId(req) || randomUUID();
    if (err.statusCode === 400 || err.statusCode === 415) {
      return envelopeFail(
        reply,
        rid,
        400,
        'BAD_REQUEST_BODY',
        'Request body is not valid JSON or has an unsupported media type',
        { reason: err.code ?? err.message },
      );
    }
    logger.child({ requestId: rid }).log({
      level: 'error',
      event: 'http.unhandled_error',
      message: err.message,
    });
    return envelopeFail(reply, rid, 500, 'INTERNAL_ERROR', 'Internal error', {
      reason: err.message,
    });
  });

  app.get('/health', async (_req, reply) => reply.code(200).send({ status: 'ok' }));

  app.post('/documents', async (req: FastifyRequest, reply: FastifyReply) => {
    const rid = req.reqId;
    const body = req.body as { id?: unknown; doc?: unknown } | undefined;
    if (!body || typeof body !== 'object') {
      return envelopeFail(reply, rid, 400, 'BAD_REQUEST_BODY', 'Body must be a JSON object', {});
    }
    if (typeof body.id !== 'string' || !DOC_ID_PATTERN.test(body.id)) {
      return envelopeFail(
        reply,
        rid,
        400,
        'BAD_REQUEST_BODY',
        'Body requires string "id" (1..128 URL-safe chars)',
        { receivedId: body.id },
      );
    }
    if (body.doc === undefined) {
      return envelopeFail(reply, rid, 400, 'BAD_REQUEST_BODY', 'Body requires a JSON "doc" value', {});
    }
    try {
      const created = service.createDocument(body.id, body.doc as JsonValue);
      return envelopeOk(reply, rid, { id: created.id, version: created.version, doc: created.doc });
    } catch (err) {
      return sendError(reply, rid, err);
    }
  });

  app.get('/documents', async (req, reply) => {
    return envelopeOk(reply, req.reqId, { documents: service.listDocuments() });
  });

  app.get<{ Params: { id: string } }>('/documents/:id', async (req, reply) => {
    const row = service.getDocument(req.params.id);
    if (!row) {
      return envelopeFail(reply, req.reqId, 404, 'DOCUMENT_NOT_FOUND', 'Document does not exist', {
        id: req.params.id,
      });
    }
    return envelopeOk(reply, req.reqId, { id: row.id, version: row.version, doc: row.doc });
  });

  app.post<{ Params: { id: string } }>('/documents/:id/patch', async (req, reply) => {
    const body = req.body as
      | { patch?: unknown; expectedVersion?: unknown; requestId?: unknown }
      | undefined;

    if (!body || typeof body !== 'object') {
      return envelopeFail(reply, req.reqId, 400, 'BAD_REQUEST_BODY', 'Body must be a JSON object', {});
    }

    // Resolve correlation id and remember whether the CLIENT chose it; only
    // client-chosen ids need the uniqueness pre-check (server UUIDs do not).
    let rid: string;
    let clientChoseId: boolean;
    if (body.requestId !== undefined) {
      if (typeof body.requestId !== 'string' || !REQUEST_ID_PATTERN.test(body.requestId)) {
        return envelopeFail(
          reply,
          req.reqId,
          400,
          'BAD_REQUEST_BODY',
          'requestId must be a 1..128-char string of [A-Za-z0-9._-]',
          { receivedRequestId: body.requestId },
        );
      }
      rid = body.requestId;
      clientChoseId = true;
    } else {
      rid = req.reqId;
      clientChoseId = headerRequestId(req) !== null;
    }

    if (!('patch' in body)) {
      return envelopeFail(reply, rid, 400, 'BAD_REQUEST_BODY', 'Body requires a "patch" array', {});
    }

    // The audit table keys on request ids; a collision must be rejected BEFORE
    // any patch executes so a duplicate id never returns a post-commit error.
    if (clientChoseId && store.getAudit(rid)) {
      return envelopeFail(
        reply,
        rid,
        400,
        'BAD_REQUEST_BODY',
        'requestId has already been used; supply a unique X-Request-Id',
        { requestId: rid },
      );
    }

    let expectedVersion: number | null = null;
    if (body.expectedVersion !== undefined && body.expectedVersion !== null) {
      if (
        typeof body.expectedVersion !== 'number' ||
        !Number.isInteger(body.expectedVersion) ||
        body.expectedVersion < 0
      ) {
        return envelopeFail(
          reply,
          rid,
          400,
          'BAD_REQUEST_BODY',
          'expectedVersion must be a non-negative integer',
          { received: body.expectedVersion },
        );
      }
      expectedVersion = body.expectedVersion;
    }

    const result = service.apply({
      docId: req.params.id,
      ops: body.patch,
      expectedVersion,
      requestId: rid,
    });

    if (!result.ok && result.failure) {
      const status = httpStatusFor(result.failure.category);
      return envelopeFail(
        reply,
        rid,
        status,
        result.failure.category,
        result.failure.message,
        result.failure.details,
        {
          docId: result.docId,
          fromVersion: result.fromVersion,
          failedAtIndex: result.failure.failedAtIndex,
          steps: result.steps,
        },
      );
    }

    return envelopeOk(
      reply,
      rid,
      {
        docId: result.docId,
        result: result.result,
        applied: result.applied,
        steps: result.steps,
      },
      { fromVersion: result.fromVersion, toVersion: result.toVersion },
    );
  });

  app.get<{ Params: { requestId: string } }>(
    '/diagnostics/requests/:requestId',
    (req, reply) => {
      const record = store.getAudit(req.params.requestId);
      if (!record) {
        return envelopeFail(
          reply,
          req.reqId,
          404,
          'AUDIT_RECORD_NOT_FOUND',
          'No audit record exists for this request id',
          { requestId: req.params.requestId },
        );
      }
      return envelopeOk(reply, req.reqId, record);
    },
  );

  app.get<{ Params: { id: string }; Querystring: { limit?: string } }>(
    '/documents/:id/history',
    (req, reply) => {
      const parsedLimit = Number.parseInt(req.query.limit ?? '20', 10);
      const limit = Number.isFinite(parsedLimit) ? Math.min(Math.max(parsedLimit, 1), 200) : 20;
      const records = store.historyForDocument(req.params.id, limit);
      return envelopeOk(reply, req.reqId, { docId: req.params.id, records });
    },
  );

  return app;
}

function httpStatusFor(category: string): number {
  const statuses: Record<string, number> = {
    MALFORMED_PATCH: 400,
    INVALID_POINTER: 400,
    ARRAY_INDEX_INVALID: 400,
    BAD_REQUEST_BODY: 400,
    POINTER_TARGET_MISSING: 422,
    POINTER_PARENT_MISSING: 422,
    ARRAY_INDEX_OUT_OF_BOUNDS: 422,
    PATH_TYPE_MISMATCH: 422,
    TEST_FAILURE: 409,
    MOVE_INTO_DESCENDANT: 409,
    VERSION_CONFLICT: 409,
    DOCUMENT_NOT_FOUND: 404,
    AUDIT_RECORD_NOT_FOUND: 404,
    INTERNAL_ERROR: 500,
  };
  return statuses[category] ?? 500;
}

function sendError(reply: FastifyReply, rid: string, err: unknown) {
  if (err instanceof PatchError) {
    return envelopeFail(reply, rid, err.httpStatus(), err.category, err.message, err.details);
  }
  const message = err instanceof Error ? err.message : String(err);
  return envelopeFail(reply, rid, 500, 'INTERNAL_ERROR', 'Internal error', { reason: message });
}
