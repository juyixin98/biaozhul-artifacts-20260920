/**
 * Fastify HTTP 服务：离线存储布局兼容检查。
 *
 * 路由：
 *   GET  /healthz              存活探针
 *   GET  /                     服务信息与用法
 *   POST /check                { old: <storageLayout>, neu: <storageLayout>, gapNamePattern?: string }
 *                              （也接受 new / newLayout 作为别名）
 *   POST /check-artifacts      { oldArtifact, newArtifact }（带 .storageLayout 的 solc artifact）
 *
 * 响应：200 + CompatReport（检查本身成功）；400 输入错误。
 */

import Fastify, { type FastifyInstance } from 'fastify';
import { compareLayouts } from './compare.js';
import type { CompatReport, SolcStorageLayout } from './types.js';

interface CheckBody {
  old?: SolcStorageLayout;
  oldLayout?: SolcStorageLayout;
  neu?: SolcStorageLayout;
  new?: SolcStorageLayout;
  newLayout?: SolcStorageLayout;
  gapNamePattern?: string;
}

interface ArtifactBody {
  oldArtifact?: { storageLayout?: SolcStorageLayout };
  newArtifact?: { storageLayout?: SolcStorageLayout };
  gapNamePattern?: string;
}

function pickLayout(body: CheckBody): { oldL?: SolcStorageLayout; newL?: SolcStorageLayout } {
  return {
    oldL: body.old ?? body.oldLayout,
    newL: body.neu ?? body.new ?? body.newLayout,
  };
}

export async function buildServer(): Promise<FastifyInstance> {
  const app = Fastify({
    logger: {
      level: process.env.LOG_LEVEL ?? 'info',
    },
  });

  app.get('/healthz', async () => ({ ok: true, service: 'storage-layout-compat' }));

  app.get('/', async () => ({
    service: 'storage-layout-compat',
    version: '1.0.0',
    endpoints: {
      'POST /check': 'body: { old: storageLayout, new: storageLayout, gapNamePattern?: regex-string }',
      'POST /check-artifacts': 'body: { oldArtifact: {storageLayout}, newArtifact: {storageLayout} }',
      'GET /healthz': 'liveness probe',
    },
    verdicts: {
      compatible: 'storage layout preserved; safe appends/gap usage reported as info',
      unknown: 'unknown types encountered or gap reserve exhausted — must not be treated as safe',
      incompatible: 'definite storage collision / move / type change',
    },
  }));

  app.post('/check', async (request, reply) => {
    const body = request.body as CheckBody | null;
    if (!body || typeof body !== 'object') {
      return reply.code(400).send({ error: 'request body must be a JSON object' });
    }
    const { oldL, newL } = pickLayout(body);
    if (!isLayout(oldL)) {
      return reply.code(400).send({ error: 'missing/invalid "old" storageLayout (need storage[] + types{})' });
    }
    if (!isLayout(newL)) {
      return reply.code(400).send({ error: 'missing/invalid "new" storageLayout (need storage[] + types{})' });
    }
    let gapRe: RegExp | undefined;
    if (body.gapNamePattern !== undefined) {
      try {
        gapRe = new RegExp(body.gapNamePattern);
      } catch (err) {
        return reply.code(400).send({ error: `invalid gapNamePattern: ${(err as Error).message}` });
      }
    }
    let report: CompatReport;
    try {
      report = compareLayouts(oldL, newL, gapRe ? { gapNamePattern: gapRe } : {});
    } catch (err) {
      // 解析/校验失败属于客户端输入问题
      request.log.warn({ err }, 'layout comparison failed');
      return reply.code(400).send({ error: (err as Error).message });
    }
    return reply.code(200).send(report);
  });

  app.post('/check-artifacts', async (request, reply) => {
    const body = request.body as ArtifactBody | null;
    if (!body || typeof body !== 'object') {
      return reply.code(400).send({ error: 'request body must be a JSON object' });
    }
    const oldL = body.oldArtifact?.storageLayout;
    const newL = body.newArtifact?.storageLayout;
    if (!isLayout(oldL)) {
      return reply.code(400).send({ error: 'oldArtifact.storageLayout invalid' });
    }
    if (!isLayout(newL)) {
      return reply.code(400).send({ error: 'newArtifact.storageLayout invalid' });
    }
    let gapRe: RegExp | undefined;
    if (body.gapNamePattern !== undefined) {
      try {
        gapRe = new RegExp(body.gapNamePattern);
      } catch (err) {
        return reply.code(400).send({ error: `invalid gapNamePattern: ${(err as Error).message}` });
      }
    }
    let report: CompatReport;
    try {
      report = compareLayouts(oldL, newL, body.gapNamePattern ? { gapNamePattern: gapRe! } : {});
    } catch (err) {
      request.log.warn({ err }, 'artifact comparison failed');
      return reply.code(400).send({ error: (err as Error).message });
    }
    return reply.code(200).send(report);
  });

  return app;
}

function isLayout(v: unknown): v is SolcStorageLayout {
  return (
    !!v &&
    typeof v === 'object' &&
    Array.isArray((v as SolcStorageLayout).storage) &&
    typeof (v as SolcStorageLayout).types === 'object' &&
    (v as SolcStorageLayout).types !== null
  );
}

async function start(): Promise<void> {
  const app = await buildServer();
  const port = Number(process.env.PORT ?? 8080);
  const host = process.env.HOST ?? '127.0.0.1';
  await app.listen({ port, host });
  app.log.info(`storage-layout-compat listening on http://${host}:${port}`);
}

// 仅作为入口模块运行时启动（被测试 import 时不启动）
if (import.meta.url === `file://${process.argv[1]}`) {
  start().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
