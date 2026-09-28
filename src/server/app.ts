/**
 * Fastify HTTP adapter.
 *
 * Routes:
 *  POST   /uploads                                   multipart intake
 *  GET    /submissions                               recent submissions
 *  GET    /submissions/:id                           one submission + parts
 *  GET    /submissions/:id/parts/:partId/blob        download a stored file
 *  GET    /diagnostics/runs                          recent run records
 *  GET    /diagnostics/runs/:runId                   one run record (replay)
 *  GET    /health                                    liveness
 *
 * The multipart body is handed to the execution kernel untouched: Fastify's
 * content parser passes the raw IncomingMessage through without buffering.
 */

import Fastify, { type FastifyInstance } from 'fastify';
import type { IncomingMessage } from 'node:http';
import {
  isMultipartError,
  type MultipartError,
} from '../protocol/errors.js';
import type { MultipartConfig } from '../protocol/config.js';
import { SubmissionRepository } from '../storage/repository.js';
import { UploadService } from '../storage/upload-service.js';
import { RunRegistry } from './diagnostics.js';

export interface AppDeps {
  config: MultipartConfig;
  repo: SubmissionRepository;
  logPath?: string;
}

export async function buildApp(deps: AppDeps): Promise<FastifyInstance> {
  // Allow up to the aggregate quota plus framing overhead (boundaries and
  // headers). The protocol kernel enforces the real byte quotas itself.
  const framingOverhead = 64 * 1024;
  const app = Fastify({
    logger: false,
    bodyLimit: deps.config.limits.maxTotalBytes + framingOverhead,
  });
  const registry = new RunRegistry();
  const uploadService = new UploadService(
    deps.config,
    deps.repo,
    deps.logPath,
    registry,
  );

  // Pass request bodies through untouched. The multipart parser is specific;
  // the wildcard fallback ensures a non-multipart body (e.g. JSON) is NOT
  // pre-parsed into an object — it reaches the kernel, which rejects the
  // envelope itself as NOT_MULTIPART (400) instead of crashing (500).
  const passThrough = (
    _req: unknown,
    payload: IncomingMessage,
    done: (err: Error | null, body?: unknown) => void,
  ): void => {
    done(null, payload);
  };
  app.removeAllContentTypeParsers();
  app.addContentTypeParser('multipart/form-data', passThrough);
  app.addContentTypeParser('*', passThrough);

  app.setErrorHandler((err, _req, reply) => {
    const mp = isMultipartError(err) ? (err as MultipartError) : null;
    if (mp) {
      void reply.status(mp.httpStatus).send({
        success: false,
        error: {
          errorClass: mp.errorClass,
          code: mp.code,
          message: mp.message,
          details: mp.details,
          ...(typeof (err as { runId?: string }).runId === 'string'
            ? { runId: (err as { runId?: string }).runId }
            : {}),
        },
      });
      return;
    }
    void reply.status(500).send({
      success: false,
      error: {
        errorClass: 'COMPUTE_ERROR',
        code: 'DB_ERROR',
        message: err.message ?? 'internal error',
        details: {},
      },
    });
  });

  app.get('/health', async () => ({ success: true, data: { status: 'ok' } }));

  app.post('/uploads', async (request, reply) => {
    const stream = request.body as IncomingMessage;
    const result = await uploadService.handleRequest(stream);
    return reply.status(201).send({
      success: true,
      data: {
        runId: result.runId,
        submission: result.submission,
      },
    });
  });

  app.get('/submissions', async (_request, reply) => {
    const submissions = deps.repo.list(20);
    return reply.send({ success: true, data: { submissions } });
  });

  app.get<{ Params: { id: string } }>('/submissions/:id', async (request, reply) => {
    const id = Number(request.params.id);
    if (!Number.isInteger(id)) {
      return reply.code(400).send({
        success: false,
        error: { errorClass: 'INPUT_ERROR', code: 'BAD_REQUEST', message: 'id must be an integer', details: {} },
      });
    }
    const submission = deps.repo.get(id);
    if (!submission) {
      return reply.code(404).send({
        success: false,
        error: { errorClass: 'INPUT_ERROR', code: 'NOT_FOUND', message: 'submission not found', details: {} },
      });
    }
    return reply.send({ success: true, data: { submission } });
  });

  app.get<{ Params: { id: string; partId: string } }>(
    '/submissions/:id/parts/:partId/blob',
    async (request, reply) => {
      const submissionId = Number(request.params.id);
      const partId = Number(request.params.partId);
      if (!Number.isInteger(submissionId) || !Number.isInteger(partId)) {
        return reply.code(400).send({ success: false, error: { message: 'ids must be integers' } });
      }
      const blob = deps.repo.getPartBlob(submissionId, partId);
      if (!blob) {
        return reply.code(404).send({ success: false, error: { message: 'file part not found' } });
      }
      return reply
        .header('content-type', 'application/octet-stream')
        .header('content-length', blob.length)
        .send(blob);
    },
  );

  app.get('/diagnostics/runs', async (_request, reply) =>
    reply.send({ success: true, data: { runs: registry.list(50) } }),
  );

  app.get<{ Params: { runId: string } }>('/diagnostics/runs/:runId', async (request, reply) => {
    const record = registry.get(request.params.runId);
    if (!record) {
      return reply.code(404).send({ success: false, error: { message: 'run not found' } });
    }
    return reply.send({ success: true, data: { run: record } });
  });

  return app;
}
