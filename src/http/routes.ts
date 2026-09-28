import { randomBytes } from 'node:crypto';
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import type {
  ByteInterval,
  ImmutableObjectMeta,
  RangeResolution,
} from '../types.js';
import { resolveRangeRequest } from '../range/core.js';
import {
  contentRangeValue,
  encodeMultipart,
  type BodyPart,
} from '../range/multipart.js';
import type { AppConfig } from '../config/index.js';
import {
  redactPreview,
  type DecisionLabel,
  type DiagnosticRecord,
  type DiagnosticsSink,
  type MemoryRingSink,
} from '../diagnostics/logger.js';
import { newRequestId } from '../diagnostics/logger.js';
import type { ObjectStore } from '../state/store.js';

/**
 * HTTP wiring. This module is intentionally thin: it translates between
 * header text / status codes and the execution core, and asks the state
 * adapter for exactly the byte intervals the core returned. All range
 * policy lives in src/range; this file must not invent its own.
 */

interface RouteDeps {
  app: FastifyInstance;
  store: ObjectStore;
  diagnostics: DiagnosticsSink;
  ring: MemoryRingSink;
  config: AppConfig;
}

function decisionLabel(decision: RangeResolution['decision']): DecisionLabel {
  switch (decision) {
    case 'SINGLE_PART':
      return 'accepted:single';
    case 'MULTIPART':
      return 'accepted:multipart';
    case 'FULL_REPRESENTATION':
      return 'accepted:full';
    case 'REJECT':
      return 'rejected';
  }
}

/** Pick a boundary whose delimiter cannot occur inside any part body. */
function chooseBoundary(parts: BodyPart[]): string {
  for (let attempt = 0; attempt < 16; attempt++) {
    const boundary = `RANGE-${randomBytes(12).toString('hex')}`;
    const delimiter = Buffer.from(`--${boundary}`);
    if (!parts.some((p) => p.bytes.includes(delimiter))) {
      return boundary;
    }
  }
  throw new Error('Failed to choose a multipart boundary absent from part bodies');
}

function baseRecord(
  req: FastifyRequest,
  objectId: string | null,
): { requestId: string; base: Omit<DiagnosticRecord, 'decision' | 'status' | 'code' | 'reason'> } {
  const requestId = (req.id as string) ?? newRequestId();
  return {
    requestId,
    base: {
      ts: new Date().toISOString(),
      requestId,
      method: req.method,
      url: req.url.split('?')[0]!,
      objectId,
      objectSize: null,
      rangeHeader: req.headers.range ?? null,
      specCount: null,
      intervals: [],
      requestedBytes: null,
      servedBytes: null,
      mergeCount: null,
      dropped: [],
    },
  };
}

export function registerRoutes({ app, store, diagnostics, ring, config }: RouteDeps): void {
  // Accept opaque bodies for local ingest; application/json still uses
  // Fastify's built-in parser (specific parsers beat the wildcard).
  app.addContentTypeParser(
    '*',
    { parseAs: 'buffer' },
    (_req, body: Buffer, done) => done(null, body),
  );

  // Expose the request id so a client can correlate a response with its
  // /diagnostics/:requestId record.
  app.addHook('onRequest', async (req, reply) => {
    reply.header('X-Request-Id', req.id);
  });

  app.get('/objects', async () => ({ objects: store.list() }));

  app.get<{ Params: { id: string } }>('/objects/:id', async (req, reply) => {
    const { id } = req.params;
    reply.header('Accept-Ranges', 'bytes');

    const meta = store.getMeta(id);
    if (meta === null) {
      emit(diagnostics, req, id, {
        decision: 'rejected',
        status: 404,
        code: 'OBJECT_NOT_FOUND',
        reason: `No object with id "${id}"`,
      });
      return reply.code(404).send({ error: 'OBJECT_NOT_FOUND', message: 'Object not found' });
    }

    reply.header('ETag', meta.etag);
    reply.header('Last-Modified', meta.lastModified.toUTCString());

    const resolution = resolveRangeRequest({
      rangeHeader: req.headers.range,
      ifRangeHeader: req.headers['if-range'] as string | undefined,
      meta,
      limits: config.limits,
    });

    return applyResolution(req, reply, meta, resolution, store, diagnostics, config);
  });

  // Local-only ingest endpoint for demos and integration seeding.
  app.post<{ Params: { id: string } }>('/objects/:id', async (req, reply) => {
    const { id } = req.params;
    const contentType = (req.headers['content-type'] as string | undefined) ?? 'application/octet-stream';
    const data = Buffer.from(req.body as Uint8Array);
    try {
      const saved = store.put({ id, data, contentType });
      return reply.code(201).send(saved);
    } catch (err) {
      return reply
        .code(409)
        .send({ error: 'OBJECT_EXISTS', message: (err as Error).message });
    }
  });

  // --- Diagnostics interface ---------------------------------------------

  app.get('/diagnostics', async (req) => {
    const query = req.query as { limit?: string; objectId?: string; decision?: string };
    return {
      records: ring.query({
        limit: query.limit === undefined ? undefined : Number(query.limit),
        objectId: query.objectId,
        decision: query.decision as DiagnosticRecord['decision'] | undefined,
      }),
    };
  });

  app.get<{ Params: { requestId: string } }>(
    '/diagnostics/:requestId',
    async (req, reply) => {
      const record = ring
        .query({ limit: 1000 })
        .find((r) => r.requestId === req.params.requestId);
      if (record === undefined) {
        return reply.code(404).send({ error: 'NOT_FOUND', message: 'Unknown request id' });
      }
      return { record };
    },
  );
}

async function applyResolution(
  req: FastifyRequest,
  reply: FastifyReply,
  meta: ImmutableObjectMeta,
  resolution: RangeResolution,
  store: ObjectStore,
  diagnostics: DiagnosticsSink,
  config: AppConfig,
): Promise<FastifyReply> {
  if (resolution.decision === 'REJECT') {
    if (resolution.status === 416) {
      // Required companion of 416: the representation's current length.
      reply.header('Content-Range', `bytes */${meta.size}`);
    }
    const payload = {
      error: resolution.code,
      message: resolution.message,
      ...(resolution.specError === undefined
        ? {}
        : { specError: resolution.specError }),
      dropped: resolution.dropped,
      objectSize: meta.size,
    };
    emit(diagnostics, req, meta.id, {
      decision: 'rejected',
      status: resolution.status,
      code: resolution.code,
      reason: resolution.message,
      objectSize: meta.size,
      dropped: resolution.dropped,
      intervals: resolution.intervals,
    });
    return reply.code(resolution.status).send(payload);
  }

  if (resolution.decision === 'FULL_REPRESENTATION') {
    const object = store.getObject(meta.id);
    if (object === null) throw new Error('Object vanished between meta lookup and read');
    reply.header('Content-Type', meta.contentType);
    reply.header('Content-Length', String(meta.size));
    emit(
      diagnostics,
      req,
      meta.id,
      {
        decision: 'accepted:full',
        status: 200,
        code: resolution.reason,
        reason: fullReason(resolution.reason),
        objectSize: meta.size,
        servedBytes: meta.size,
      },
      config.logBodies ? object.data : undefined,
    );
    return reply.code(200).send(object.data);
  }

  // 206 paths: read exactly the resolved intervals at original offsets.
  const parts: BodyPart[] = [];
  for (const interval of resolution.intervals as ByteInterval[]) {
    const bytes = store.getSlice(meta.id, interval.start, interval.end);
    if (bytes === null) throw new Error('Slice read failed after successful meta lookup');
    parts.push({ interval, bytes });
  }

  if (resolution.decision === 'SINGLE_PART') {
    const { interval, bytes } = parts[0]!;
    reply.header('Content-Type', meta.contentType);
    reply.header('Content-Range', contentRangeValue(interval, meta.size));
    reply.header('Content-Length', String(bytes.length));
    emit(diagnostics, req, meta.id, {
      decision: 'accepted:single',
      status: 206,
      code: 'OK',
      reason: `Serving bytes ${interval.start}-${interval.end} of ${meta.size}`,
      objectSize: meta.size,
      specCount: specCountOf(req),
      intervals: resolution.intervals,
      requestedBytes: resolution.requestedBytes,
      servedBytes: resolution.servedBytes,
      mergeCount: resolution.mergeCount,
      dropped: resolution.dropped,
    }, config.logBodies ? bytes : undefined);
    return reply.code(206).send(bytes);
  }

  const boundary = chooseBoundary(parts);
  const { contentType, body } = encodeMultipart(parts, meta.size, meta.contentType, boundary);
  reply.header('Content-Type', contentType);
  reply.header('Content-Length', String(body.length));
  emit(diagnostics, req, meta.id, {
    decision: 'accepted:multipart',
    status: 206,
    code: 'OK',
    reason: `Serving ${parts.length} merged part(s), ${resolution.mergeCount} merge(s), ${body.length} framed bytes`,
    objectSize: meta.size,
    specCount: specCountOf(req),
    intervals: resolution.intervals,
    requestedBytes: resolution.requestedBytes,
    servedBytes: resolution.servedBytes,
    mergeCount: resolution.mergeCount,
    dropped: resolution.dropped,
  }, config.logBodies ? body : undefined);
  return reply.code(206).send(body);
}

function specCountOf(req: FastifyRequest): number | null {
  const range = req.headers.range;
  return range === undefined ? null : range.split(',').length;
}

function fullReason(code: string): string {
  switch (code) {
    case 'NO_RANGE_HEADER':
      return 'No Range header: returning the full representation';
    case 'UNSUPPORTED_UNIT_IGNORED':
      return 'Unsupported range unit ignored per RFC 9110: returning the full representation';
    case 'IF_RANGE_MISMATCH':
      return 'If-Range precondition failed (stale validator): returning the full representation';
    case 'IF_RANGE_INDETERMINATE':
      return 'If-Range could not be evaluated: conservatively returning the full representation';
    default:
      return code;
  }
}

function emit(
  sink: DiagnosticsSink,
  req: FastifyRequest,
  objectId: string | null,
  fields: Partial<DiagnosticRecord> & {
    decision: DecisionLabel;
    status: number;
    code: string;
    reason: string;
  },
  bodyForPreview?: Buffer,
): void {
  const { requestId, base } = baseRecord(req, objectId);
  void requestId;
  const record: DiagnosticRecord = {
    ...base,
    ...fields,
    intervals: fields.intervals ?? [],
    dropped: fields.dropped ?? [],
    ...(bodyForPreview === undefined ? {} : { bodyPreview: redactPreview(bodyForPreview) }),
  };
  sink.record(record);
}
