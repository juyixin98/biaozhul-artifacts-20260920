/**
 * Diagnostics HTTP layer (Fastify).
 *
 * Routes:
 *   POST /api/v1/diffs          analyze an {old, new} contract pair
 *   GET  /api/v1/diffs/:id      re-fetch a stored analysis by request id
 *   GET  /api/v1/runs           list recent runs
 *   GET  /health                liveness probe
 *
 * Every response (and log line) carries the request id. Analysis steps,
 * versions, locations, failure reasons and uncertainties are emitted as
 * structured log fields so a failed run is reproducible from logs alone.
 */
import Fastify, { type FastifyInstance } from 'fastify';
import { randomUUID } from 'node:crypto';
import { DiffEngine } from '../kernel/diff-engine.js';
import { DiffStore } from '../state/diff-store.js';
import { config } from '../config.js';
import type { DiffResult } from '../core/types.js';

export interface AppDeps {
  store: DiffStore;
  engine?: DiffEngine;
}

export async function buildApp(deps: AppDeps): Promise<FastifyInstance> {
  const engine = deps.engine ?? new DiffEngine();
  const app = Fastify({
    logger: { level: config.logLevel },
    bodyLimit: config.maxDocumentBytes,
  });

  app.addHook('onRequest', async (req, reply) => {
    const headerId = typeof req.headers['x-request-id'] === 'string'
      ? req.headers['x-request-id']
      : undefined;
    const requestId = headerId && /^[A-Za-z0-9._:-]{1,128}$/.test(headerId)
      ? headerId
      : randomUUID();
    req.log = req.log.child({ requestId });
    reply.header('x-request-id', requestId);
    (req as unknown as { diffRequestId: string }).diffRequestId = requestId;
  });

  app.get('/health', async () => ({ status: 'ok' }));

  app.post('/api/v1/diffs', async (req, reply) => {
    const requestId = (req as unknown as { diffRequestId: string }).diffRequestId;
    const body = req.body as { old?: unknown; new?: unknown } | undefined;

    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      req.log.warn({ requestId }, 'rejected: body must be a JSON object');
      return reply.code(400).send(errorEnvelope('body must be a JSON object {old,new}', requestId));
    }
    if (typeof body.old !== 'object' || body.old === null || Array.isArray(body.old)
      || typeof body.new !== 'object' || body.new === null || Array.isArray(body.new)) {
      req.log.warn({ requestId }, 'rejected: old/new must be OpenAPI objects');
      return reply.code(400).send(errorEnvelope('fields "old" and "new" must be OpenAPI document objects', requestId));
    }

    req.log.info({ requestId }, 'contract diff analysis started');
    const { result, error } = engine.diff(body.old, body.new, requestId);

    for (const step of result.steps) {
      req.log.debug(
        { requestId, step: step.index, phase: step.phase, location: step.location },
        step.detail,
      );
    }
    if (result.uncertainties.length > 0) {
      // uncertainties are listed separately from hard failures, by design
      req.log.warn({ requestId, uncertainties: result.uncertainties }, 'analysis produced uncertainties');
    }
    if (error) {
      req.log.error({ requestId, error: error.message }, 'contract could not be parsed');
      return reply.code(422).send(errorEnvelope(error.message, requestId, result.uncertainties));
    }

    deps.store.saveRun(result);
    logVerdict(req.log, result);
    return reply.code(200).send(successEnvelope(result));
  });

  app.get<{ Params: { id: string } }>('/api/v1/diffs/:id', async (req, reply) => {
    const requestId = req.params.id;
    const stored = deps.store.getRun(requestId);
    if (!stored) {
      return reply.code(404).send(errorEnvelope(`no analysis found for request id ${requestId}`, requestId));
    }
    const result: DiffResult = {
      requestId,
      oldTitle: stored.old_title,
      newTitle: stored.new_title,
      oldVersion: stored.old_version,
      newVersion: stored.new_version,
      compatible: stored.compatible === 1,
      findings: stored.findings,
      uncertainties: stored.uncertainties,
      steps: [],
      stats: {
        operationsCompared: -1,
        operationsAdded: -1,
        operationsRemoved: -1,
        breaking: stored.breaking,
        nonBreaking: stored.non_breaking,
        uncertain: stored.uncertain,
      },
    };
    return reply.send(successEnvelope(result, { persisted: true }));
  });

  app.get('/api/v1/runs', async () => ({
    success: true,
    data: { runs: deps.store.listRuns() },
  }));

  return app;
}

type LoggerLike = {
  info: (obj: object, msg?: string) => void;
  warn: (obj: object, msg?: string) => void;
  error: (obj: object, msg?: string) => void;
  debug: (obj: object, msg?: string) => void;
};

function logVerdict(log: LoggerLike, result: DiffResult): void {
  const summary = {
    requestId: result.requestId,
    versions: { old: result.oldVersion, new: result.newVersion },
    compatible: result.compatible,
    stats: result.stats,
    breakingCodes: result.findings.filter((f) => f.severity === 'BREAKING').map((f) => f.code),
  };
  if (result.compatible) {
    log.info(summary, 'contract diff analysis completed: compatible');
  } else {
    log.warn(summary, 'contract diff analysis completed: incompatible');
  }
}

function successEnvelope(result: DiffResult, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { success: true, requestId: result.requestId, data: { result, ...extra } };
}

function errorEnvelope(
  message: string,
  requestId: string,
  uncertainties: DiffResult['uncertainties'] = [],
): Record<string, unknown> {
  return { success: false, requestId, error: { message }, uncertainties };
}
