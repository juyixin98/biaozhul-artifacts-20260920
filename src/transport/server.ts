import Fastify, { type FastifyInstance } from 'fastify';
import type { Kernel } from '../kernel/kernel.js';
import { newConnectionId } from '../kernel/ids.js';
import type { TaskRegistry } from '../kernel/task-registry.js';

/**
 * HTTP transport. Its only jobs are:
 *  - read the raw body (parsing is the contract layer's job, not Fastify's),
 *  - bind a connection id + AbortSignal to each request,
 *  - map the kernel result to HTTP status/body (204 for notification-only
 *    batches, otherwise 200 with the JSON-RPC payload).
 */
export interface BuildServerOptions {
  kernel: Kernel;
  tasks: TaskRegistry;
  /** Structured logger level forwarded to Fastify. */
  fastifyLoggerLevel?: 'silent' | 'info' | 'debug';
}

export function buildServer(opts: BuildServerOptions): FastifyInstance {
  const app = Fastify({
    // Keep the HTTP-level cap ABOVE the contract layer's cap (1 MiB, enforced
    // in parse.ts as a -32700 JSON-RPC error). Otherwise Fastify would answer
    // an oversized body with a transport 413 and bypass the parse-error layer.
    bodyLimit: 2 * 1024 * 1024,
    logger:
      opts.fastifyLoggerLevel === undefined || opts.fastifyLoggerLevel === 'silent'
        ? false
        : { level: opts.fastifyLoggerLevel === 'debug' ? 'debug' : 'info' },
  });

  app.addContentTypeParser(
    'application/json',
    { parseAs: 'string' },
    (_req, body, done) => done(null, body),
  );
  // Be permissive about charset suffixes and missing type, but still get text.
  app.addContentTypeParser('*', { parseAs: 'string' }, (_req, body, done) => done(null, body));

  app.get('/healthz', async () => ({ ok: true, liveTasks: opts.tasks.liveCount() }));

  app.post('/', async (req, reply) => {
    const connectionId = newConnectionId();
    const controller = new AbortController();

    // A request must only count as "lost" if the connection drops BEFORE we
    // finish sending the response. Node fires IncomingMessage 'close' as soon
    // as the request body has been fully read (which Fastify already did by
    // the time this handler runs), so binding that naively aborts every
    // request immediately. We gate on the socket actually closing while the
    // response has not yet been written.
    let responded = false;
    const onConnectionGone = (): void => {
      if (!responded && !controller.signal.aborted) {
        controller.abort(new Error('client closed connection'));
      }
    };
    const socket = req.raw.socket;
    socket.on('close', onConnectionGone);
    req.raw.on('aborted', onConnectionGone);

    const rawBody = typeof req.body === 'string' ? req.body : '';
    if (rawBody.length === 0) {
      // Empty body is a parse error, not an HTTP 400: there is no envelope.
      responded = true;
      socket.removeListener('close', onConnectionGone);
      req.raw.removeListener('aborted', onConnectionGone);
      await reply
        .code(200)
        .type('application/json')
        .send({
          jsonrpc: '2.0',
          error: { code: -32700, message: 'Parse error: empty request body' },
          id: null,
        });
      return;
    }

    const result = await opts.kernel.handle(rawBody, {
      connectionId,
      signal: controller.signal,
    });

    // The response (including a detached task's immediate "pending" reply) is
    // about to be written. A later socket close — e.g. the client leaving
    // after receiving it — must NOT cancel background work.
    responded = true;
    socket.removeListener('close', onConnectionGone);
    req.raw.removeListener('aborted', onConnectionGone);

    if (result.status === 204 || result.payload === null) {
      await reply.code(204).send();
      return;
    }
    await reply.code(200).type('application/json').send(result.payload);
  });

  return app;
}
