/**
 * Fastify transport: HTTP mapping onto the kernel.
 *
 * The layer is intentionally thin — all conditional semantics live in the
 * contract/core layers. Responsibilities here: parse JSON, extract condition
 * headers, map kernel results to status codes, attach validator headers from
 * the committed snapshot, and translate every failure category into an
 * explicit error envelope (unknown errors are 500, never 2xx).
 */
import Fastify, { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import {
  DomainError,
  InvalidJsonError,
  MalformedConditionError,
  NotFoundError,
  PreconditionFailedError,
  TraceStep
} from '../contract/errors.js';
import { formatETag } from '../contract/etag.js';
import { formatHttpDate } from '../contract/http-date.js';
import { ResourceKernel, WriteOutcome } from '../core/kernel.js';
import { ResourceStore } from '../core/ports.js';
import { etagFor } from '../core/versioning.js';
import { JsonRunLogger, RunLogger, newRunId, traceToLog } from './logger.js';

const RESOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const MAX_BODY_BYTES = 1 * 1024 * 1024;

interface IdParams {
  readonly id: string;
}

interface VersionParams extends IdParams {
  readonly version: string;
}

interface ListQuery {
  readonly limit?: string;
  readonly offset?: string;
}

interface RequestContext {
  readonly runId: string;
  readonly log: RunLogger;
}

type ContextualRequest = FastifyRequest & { ctx: RequestContext };

export interface AppDeps {
  readonly store: ResourceStore;
  readonly kernel: ResourceKernel;
  readonly logs: JsonRunLogger;
}

export function buildApp(deps: AppDeps): FastifyInstance {
  const { store, kernel, logs } = deps;
  const app = Fastify({ logger: false, bodyLimit: MAX_BODY_BYTES });

  // Parse JSON ourselves so a syntax error becomes the domain's 400 category
  // instead of Fastify's generic parser error.
  app.addContentTypeParser('application/json', { parseAs: 'string' }, (_req, body: string, done) => {
    if (body === '') {
      // A JSON content type with no bytes is an invalid JSON document, not a
      // representation of null (clients must send the literal "null" for that).
      done(new InvalidJsonError('empty request body'));
      return;
    }
    try {
      done(null, JSON.parse(body));
    } catch (err) {
      done(new InvalidJsonError((err as Error).message));
    }
  });
  app.addContentTypeParser('application/merge-patch+json', { parseAs: 'string' }, (_req, body: string, done) => {
    if (body === '') {
      done(new InvalidJsonError('empty merge-patch body'), undefined);
      return;
    }
    try {
      done(null, JSON.parse(body));
    } catch (err) {
      done(new InvalidJsonError((err as Error).message));
    }
  });

  // Bind a run identity per request. A request-supplied X-Request-Id is
  // honored (so replay fixtures correlate with logs); otherwise one is issued.
  app.addHook('onRequest', (req: FastifyRequest, reply: FastifyReply, done) => {
    const incoming = req.headers['x-request-id'];
    const runId = typeof incoming === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(incoming) ? incoming : newRunId();
    reply.header('X-Request-Id', runId);
    (req as ContextualRequest).ctx = { runId, log: logs.forRun(runId) };
    done();
  });

  // Writes require an explicit JSON representation; no body / wrong content
  // type must surface as a 400 rather than reach storage as undefined.
  app.addHook('preHandler', (req: FastifyRequest, _reply: FastifyReply, done: (err?: Error) => void) => {
    if ((req.method === 'PUT' || req.method === 'PATCH') && req.body === undefined) {
      done(new InvalidJsonError('request must include a JSON body with a JSON content type'));
      return;
    }
    done();
  });

  // --- Collection --------------------------------------------------------
  app.get('/resources', (req: FastifyRequest, reply: FastifyReply) => {
    const { log } = (req as ContextualRequest).ctx;
    const query = req.query as ListQuery;
    const limit = clampInt(query.limit, 50, 1, 200);
    const offset = clampInt(query.offset, 0, 0, 1_000_000);
    const rows = store.listCurrent(limit, offset);
    log.info('collection.list', { limit, offset, returned: rows.length });
    reply.status(200).send({
      items: rows.map((row) => ({
        id: row.resourceId,
        version: row.version,
        body: row.body,
        etag: etagFromVersion(row.version, row.body),
        lastModified: formatHttpDate(row.createdMs),
        links: { self: `/resources/${row.resourceId}`, history: `/resources/${row.resourceId}/versions` }
      }))
    });
  });

  // --- Reads -------------------------------------------------------------
  // Fastify automatically serves HEAD for a GET route; branch inside one
  // handler so a HEAD request runs the same conditional logic but has no body.
  const handleRead = (req: FastifyRequest<{ Params: IdParams; Querystring: { weak?: string } }>, reply: FastifyReply): void => {
    const { log } = (req as ContextualRequest).ctx;
    const { id } = req.params;
    // Explicit opt-in for a weak validator on an otherwise byte-identical
    // representation, so the weak-validator round trip is demonstrable. The
    // server emits strong validators by default (stored JSON is deterministic).
    const weakEtag = req.query.weak === '1' || req.query.weak === 'true';
    if (!RESOURCE_ID.test(id)) throw new NotFoundError(`Unknown resource id "${id}"`);
    const outcome = kernel.get(id, extractConditions(req));
    if (!outcome) {
      // Absence and a mismatched version are intentionally distinct:
      // unknown resource is 404; stale If-Match on an existing one is 412.
      throw new NotFoundError(`Resource "${id}" does not exist`);
    }
    const { representation } = outcome;
    const etagHeader = weakEtag
      ? representation.etagHeader.replace(/^"/, 'W/"')
      : representation.etagHeader;
    logTrace(log, 'read.conditions', outcome.trace);
    reply
      .header('ETag', etagHeader)
      .header('Last-Modified', formatHttpDate(representation.snapshot.createdMs))
      .header('X-Resource-Version', String(representation.snapshot.version));

    if (outcome.kind === 'not-modified') {
      log.info('read.not_modified', { id, version: representation.snapshot.version, weakEtag });
      reply.status(304).send();
      return;
    }
    log.info('read.ok', { id, version: representation.snapshot.version, weakEtag });
    if (req.method === 'HEAD') {
      reply.status(200).send();
      return;
    }
    reply.status(200).send(envelope(id, representation.snapshot.version, representation.snapshot.body));
  };

  app.get<{ Params: IdParams; Querystring: { weak?: string } }>('/resources/:id', (req, reply) => handleRead(req, reply));

  // --- Writes ------------------------------------------------------------
  app.put<{ Params: IdParams }>('/resources/:id', (req, reply) => {
    const { id } = req.params;
    if (!RESOURCE_ID.test(id)) throw new MalformedConditionError(':id', id, 'unsupported characters in resource id');
    const outcome = kernel.put(id, req.body, extractConditions(req));
    return sendWrite((req as ContextualRequest).ctx.log, reply, outcome, id, 'put');
  });

  app.patch<{ Params: IdParams }>('/resources/:id', (req, reply) => {
    const { id } = req.params;
    if (!RESOURCE_ID.test(id)) throw new MalformedConditionError(':id', id, 'unsupported characters in resource id');
    const outcome = kernel.patch(id, req.body, extractConditions(req));
    return sendWrite((req as ContextualRequest).ctx.log, reply, outcome, id, 'patch');
  });

  app.delete<{ Params: IdParams }>('/resources/:id', (req, reply) => {
    const { id } = req.params;
    if (!RESOURCE_ID.test(id)) throw new NotFoundError(`Unknown resource id "${id}"`);
    const outcome = kernel.remove(id, extractConditions(req));
    return sendWrite((req as ContextualRequest).ctx.log, reply, outcome, id, 'delete');
  });

  // --- Diagnostics: immutable version history ---------------------------
  app.get<{ Params: IdParams }>('/resources/:id/versions', (req, reply) => {
    const { id } = req.params;
    const versions = store.listVersions(id);
    if (versions.length === 0) throw new NotFoundError(`No history for resource "${id}"`);
    reply.status(200).send({
      id,
      versions: versions.map((v) => ({
        version: v.version,
        deleted: v.deleted,
        created: formatHttpDate(v.createdMs),
        body: v.deleted ? null : v.body,
        links: { self: `/resources/${id}/versions/${v.version}` }
      }))
    });
  });

  app.get<{ Params: VersionParams }>('/resources/:id/versions/:version', (req, reply) => {
    const { id, version: versionParam } = req.params;
    const version = Number(versionParam);
    if (!Number.isInteger(version) || version < 1) {
      throw new MalformedConditionError(':version', versionParam, 'expected a positive integer');
    }
    const row = store.selectVersion(id, version);
    if (!row) throw new NotFoundError(`Version ${version} of "${id}" does not exist`);
    reply.status(200).send({
      id,
      version: row.version,
      deleted: row.deleted,
      created: formatHttpDate(row.createdMs),
      body: row.deleted ? null : row.body
    });
  });

  app.get('/healthz', (_req, reply) => {
    reply.status(200).send({ status: 'ok', resources: store.countResources() });
  });

  // --- Error mapping -----------------------------------------------------
  app.setErrorHandler((err: unknown, req: FastifyRequest, reply: FastifyReply) => {
    const ctx = (req as Partial<ContextualRequest>).ctx;
    const log = ctx?.log ?? logs.forRun('unknown');
    if (err instanceof PreconditionFailedError) {
      log.warn('precondition.failed', { code: err.code, trace: traceToLog(err.trace) });
      if (err.currentValidator) {
        reply.header('ETag', err.currentValidator.etag);
        reply.header('Last-Modified', err.currentValidator.lastModified);
      }
      reply.status(412).send(errorBody(err.code, err.message, err.trace));
      return;
    }
    if (err instanceof NotFoundError) {
      log.info('not_found', { code: err.code, path: req.url });
      reply.status(404).send(errorBody(err.code, err.message));
      return;
    }
    if (err instanceof MalformedConditionError || err instanceof InvalidJsonError) {
      log.warn('client.bad_request', { code: err.code, message: err.message });
      reply.status(400).send(errorBody(err.code, err.message));
      return;
    }
    if (err instanceof DomainError) {
      log.error('domain.error', { code: err.code, message: err.message });
      reply.status(err.statusCode).send(errorBody(err.code, err.message));
      return;
    }
    if (isFastifyClientError(err)) {
      log.warn('client.rejected', { message: err.message });
      reply.status(err.statusCode).send(errorBody('MALFORMED_CONDITION_HEADER', err.message));
      return;
    }
    log.error('unhandled.error', {
      message: err instanceof Error ? err.message : String(err),
      stack: err instanceof Error ? err.stack : undefined
    });
    reply.status(500).send(errorBody('INTERNAL_ERROR', 'Internal server error'));
  });

  return app;
}

// --- helpers --------------------------------------------------------------

function extractConditions(req: FastifyRequest): {
  ifMatch?: string;
  ifNoneMatch?: string;
  ifModifiedSince?: string;
  ifUnmodifiedSince?: string;
} {
  const h = req.headers;
  return {
    ifMatch: singleHeader(h['if-match']),
    ifNoneMatch: singleHeader(h['if-none-match']),
    ifModifiedSince: singleHeader(h['if-modified-since']),
    ifUnmodifiedSince: singleHeader(h['if-unmodified-since'])
  };
}

function singleHeader(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function clampInt(raw: string | undefined, fallback: number, min: number, max: number): number {
  if (raw === undefined) return fallback;
  const n = Number(raw);
  if (!Number.isInteger(n)) return fallback;
  return Math.min(Math.max(n, min), max);
}

function envelope(id: string, version: number, body: unknown): unknown {
  return { id, version, body };
}

function errorBody(code: string, message: string, trace?: readonly TraceStep[]): unknown {
  return { error: { code, message, trace: trace ? traceToLog(trace) : undefined } };
}

function sendWrite(
  log: RunLogger,
  reply: FastifyReply,
  outcome: WriteOutcome,
  id: string,
  op: 'put' | 'patch' | 'delete'
): FastifyReply {
  logTrace(log, `${op}.conditions`, outcome.trace);
  if (outcome.status === 204) {
    log.info(`${op}.deleted`, { id });
    return reply.status(204).send();
  }
  const rep = outcome.representation;
  if (!rep) {
    // Defensive: 200/201 without a snapshot is an implementation bug, not a
    // client-visible success.
    log.error(`${op}.missing_snapshot`, { id });
    return reply.status(500).send(errorBody('INTERNAL_ERROR', 'Committed snapshot unavailable'));
  }
  reply
    .header('ETag', rep.etagHeader)
    .header('Last-Modified', formatHttpDate(rep.snapshot.createdMs))
    .header('X-Resource-Version', String(rep.snapshot.version));
  log.info(`${op}.ok`, { id, status: outcome.status, version: rep.snapshot.version });
  return reply.status(outcome.status).send(envelope(id, rep.snapshot.version, rep.snapshot.body));
}

function logTrace(log: RunLogger, event: string, trace: readonly TraceStep[]): void {
  if (trace.length > 0) log.info(event, { steps: traceToLog(trace) });
}

function etagFromVersion(version: number, body: unknown): string {
  const tag = etagFor(version, body);
  return formatETag(tag.opaque, tag.weak);
}

function isFastifyClientError(err: unknown): err is Error & { statusCode: number } {
  if (!(err instanceof Error)) return false;
  const status = (err as { statusCode?: unknown }).statusCode;
  return typeof status === 'number' && status >= 400 && status < 500;
}
