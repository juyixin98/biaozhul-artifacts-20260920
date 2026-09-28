/**
 * 服务入口：Fastify + 本地合成 SQLite 夹具。
 *   PORT=3000 npm start
 */
import { createApp } from './app.js';

const PORT = Number.parseInt(process.env.PORT ?? '3000', 10);
const LOG_PATH = process.env.RUN_LOG ?? 'logs/runs.jsonl';

async function main(): Promise<void> {
  const { app, adapter } = createApp(LOG_PATH);
  await app.listen({ port: PORT, host: '0.0.0.0' });
  app.log.info(`budget gateway listening on http://0.0.0.0:${PORT} (run log: ${LOG_PATH})`);

  const shutdown = async (): Promise<void> => {
    await app.close();
    adapter.close();
    process.exit(0);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
