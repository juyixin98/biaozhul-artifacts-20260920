import Fastify, { FastifyInstance } from 'fastify';
import { compareLayouts } from './compare.js';
import { CompareReport, StorageLayout } from './types.js';

interface CompareBody {
  old?: StorageLayout;
  new?: StorageLayout;
}

function isLayout(v: unknown): v is StorageLayout {
  if (typeof v !== 'object' || v === null) return false;
  const l = v as Record<string, unknown>;
  return Array.isArray(l['storage']) && typeof l['types'] === 'object' && l['types'] !== null;
}

/** Build the HTTP service. Exported separately from `listen` for testability. */
export function buildServer(): FastifyInstance {
  const app = Fastify({ logger: true });

  app.get('/health', async () => ({ status: 'ok' }));

  app.post('/compare', async (request, reply) => {
    const body = request.body as CompareBody | undefined;
    if (!body || !isLayout(body.old) || !isLayout(body.new)) {
      return reply.code(400).send({
        error: 'bad_request',
        message:
          'body must be JSON: { "old": <storageLayout>, "new": <storageLayout> }, ' +
          'each with "storage" (array) and "types" (object) as emitted by solc',
      });
    }
    const report: CompareReport = compareLayouts(body.old, body.new);
    return reply.code(report.verdict === 'incompatible' ? 422 : 200).send(report);
  });

  return app;
}

const isMain = process.argv[1] !== undefined && import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  const port = Number(process.env['PORT'] ?? 3000);
  const host = process.env['HOST'] ?? '0.0.0.0';
  buildServer()
    .listen({ port, host })
    .catch((err) => {
      console.error(err);
      process.exit(1);
    });
}
