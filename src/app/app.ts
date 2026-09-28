/**
 * Fastify application: HTTP boundary.
 *
 *  - POST /upload                 streaming multipart intake (commit-gated)
 *  - GET  /healthz                liveness + store counters
 *  - GET  /diagnostics/limits     effective limits & file policy
 *  - GET  /diagnostics/runs       tail of the JSONL run log (replay support)
 *  - GET  /submissions            recent committed submissions
 *  - GET  /submissions/:id        one committed submission (404 if not visible)
 *  - GET  /submissions/:id/file/:partIndex  download a committed file part
 */

import { createReadStream, existsSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { readFile } from 'node:fs/promises';
import Fastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from 'fastify';
import { ErrorCode, MultipartError } from '../protocol/errors.js';
import type { AppConfig } from '../config.js';
import { RunLogger } from '../diagnostics/run-logger.js';
import { SubmissionStore } from '../storage/submission-store.js';
import { processUpload } from './upload-service.js';

export interface AppDeps {
  config: AppConfig;
  store: SubmissionStore;
  logger: RunLogger;
}

export function buildApp(deps: AppDeps): FastifyInstance {
  const { config, store, logger } = deps;
  const app = Fastify({
    logger: false,
    bodyLimit: Number.MAX_SAFE_INTEGER
  });

  // Media-type gate: anything other than multipart/form-data is 415 at the
  // boundary, before the entity stream is touched.
  const requireMultipart = async (request: FastifyRequest, reply: FastifyReply): Promise<unknown> => {
    const ct = request.headers['content-type'];
    if (typeof ct !== 'string' || !/^multipart\/form-data(?:\s|;|$)/i.test(ct)) {
      return reply.status(415).send({
        error: 'UNSUPPORTED_MEDIA_TYPE',
        errorClass: 'INPUT_ERROR',
        message: '/upload requires Content-Type: multipart/form-data',
        httpStatus: 415
      });
    }
    return undefined;
  };

  // Fastify invokes this parser with the raw payload stream and does not
  // consume it when parseAs is unset; calling done(null, null) defers all
  // parsing to the route handler, which streams request.raw through our own
  // multipart parser instead of buffering the entity.
  app.addContentTypeParser('multipart/form-data', (_request, _payload, done) => {
    done(null, null);
  });

  app.setErrorHandler((error, _request, reply) => {
    if (error instanceof MultipartError) {
      // A parse failure leaves the request entity partially unread; close the
      // connection after delivering the structured error so no bytes can leak
      // onto a subsequent request on a reused socket.
      void reply.header('connection', 'close').status(error.httpStatus).send(error.toJSON());
      return;
    }
    // Fastify's own errors (e.g. unsupported media type, 404) keep their code.
    const status = (error as { statusCode?: number }).statusCode ?? 500;
    void reply.status(status).send({
      error: status >= 500 ? 'INTERNAL' : 'HTTP_ERROR',
      errorClass: status >= 500 ? 'COMPUTE_FAILURE' : 'INPUT_ERROR',
      message: (error as Error).message,
      httpStatus: status
    });
  });

  app.get('/healthz', async () => ({
    status: 'ok',
    ...store.count()
  }));

  app.get('/diagnostics/limits', async () => ({
    limits: config.limits,
    filePolicy: config.filePolicy,
    requireFilePart: config.requireFilePart
  }));

  app.get('/diagnostics/runs', async (request) => {
    const q = request.query as { tail?: string };
    const tail = Math.min(500, Math.max(1, Number.parseInt(q.tail ?? '50', 10) || 50));
    if (!logger.path) return { runLogPath: null, entries: [] };
    let lines: string[] = [];
    try {
      const raw = await readFile(logger.path, 'utf8');
      lines = raw.split('\n').filter((l) => l.trim() !== '');
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err;
    }
    const entries = lines
      .slice(-tail)
      .map((l) => {
        try {
          return JSON.parse(l) as unknown;
        } catch {
          return { malformed: true, raw: l };
        }
      });
    return { runLogPath: logger.path, count: entries.length, entries };
  });

  app.post('/upload', { onRequest: [requireMultipart] }, async (request, reply) => {
    const outcome = await processUpload(request.raw, config, store, logger);
    return reply.status(201).send({
      runId: outcome.runId,
      submission: {
        id: outcome.record.id,
        createdAt: outcome.record.createdAt,
        totalBodyBytes: outcome.record.totalBodyBytes,
        partsCount: outcome.record.partsCount,
        fieldsCount: outcome.record.fieldsCount,
        filesCount: outcome.record.filesCount,
        parts: outcome.record.parts.map((p) =>
          p.isFile
            ? {
                partIndex: p.partIndex,
                field: p.name,
                type: 'file',
                filename: p.filename,
                storedName: p.storedName,
                contentType: p.contentType,
                size: p.size,
                sha256: p.sha256,
                href: `/submissions/${outcome.record.id}/file/${p.partIndex}`
              }
            : {
                partIndex: p.partIndex,
                field: p.name,
                type: 'field',
                contentType: p.contentType,
                size: p.size,
                sha256: p.sha256,
                value: p.value
              }
        )
      }
    });
  });

  app.get('/submissions', async (request) => {
    const q = request.query as { limit?: string };
    const limit = Math.min(200, Math.max(1, Number.parseInt(q.limit ?? '50', 10) || 50));
    return { submissions: store.listRecent(limit) };
  });

  app.get('/submissions/:id', async (request, reply) => {
    const params = request.params as { id: string };
    const record = store.getSubmission(params.id);
    if (!record) {
      return reply.status(404).send({
        error: 'NOT_FOUND',
        errorClass: 'INPUT_ERROR',
        message: `submission ${params.id} does not exist (or is not yet committed)`,
        httpStatus: 404
      });
    }
    return record;
  });

  // Dedicated 404 mapping without abusing an unrelated error code.
  app.setNotFoundHandler((_request, reply) => {
    void reply.status(404).send({
      error: 'NOT_FOUND',
      errorClass: 'INPUT_ERROR',
      message: 'resource not found',
      httpStatus: 404
    });
  });

  app.get('/submissions/:id/file/:partIndex', async (request, reply) => {
    const params = request.params as { id: string; partIndex: string };
    const partIndex = Number.parseInt(params.partIndex, 10);
    const record = store.getSubmission(params.id);
    if (!record) {
      return reply.status(404).send({
        error: 'NOT_FOUND',
        errorClass: 'INPUT_ERROR',
        message: `submission ${params.id} does not exist (or is not yet committed)`,
        httpStatus: 404
      });
    }
    const part = record.parts.find((p) => p.partIndex === partIndex && p.isFile);
    if (!part || !part.storedName) {
      return reply.status(404).send({
        error: 'NOT_FOUND',
        errorClass: 'INPUT_ERROR',
        message: `file part ${partIndex} not found in submission ${params.id}`,
        httpStatus: 404
      });
    }
    const filePath = join(config.filesDir, part.storedName);
    if (!existsSync(filePath)) {
      return reply.status(410).send({
        error: 'FILE_GONE',
        errorClass: 'COMPUTE_FAILURE',
        message: 'committed file is missing from storage',
        httpStatus: 410
      });
    }
    const size = statSync(filePath).size;
    reply
      .header('Content-Type', part.contentType ?? 'application/octet-stream')
      .header(
        'Content-Disposition',
        `attachment; filename*=UTF-8''${encodeRFC5987(part.filename ?? part.storedName)}`
      )
      .header('Content-Length', size)
      .header('X-Content-SHA256', part.sha256);
    return reply.send(createReadStream(filePath));
  });

  return app;
}

function encodeRFC5987(value: string): string {
  // encodeURIComponent leaves a few RFC 5987 sub-delims unescaped; escape them.
  return encodeURIComponent(value).replace(/['()*]/g, (c) =>
    '%' + c.charCodeAt(0).toString(16).toUpperCase()
  );
}
