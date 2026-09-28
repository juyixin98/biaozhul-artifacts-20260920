/**
 * 可运行服务入口：npm start
 * 端口取 PORT（默认 3000）。数据全部来自本地合成夹具的内存 SQLite。
 */
import { buildApp } from './bootstrap.js';

const port = Number(process.env.PORT ?? 3000);
const built = buildApp();

built.app.listen({ port, host: '127.0.0.1' })
  .then(() => {
    process.stdout.write(
      `typed-query budget gateway listening on http://127.0.0.1:${port}\n` +
        `endpoints: POST /query/explain, POST /query, GET /runs, GET /runs/:id\n`,
    );
  })
  .catch((err: unknown) => {
    process.stderr.write(`failed to start: ${err instanceof Error ? err.message : String(err)}\n`);
    process.exit(1);
  });

const shutdown = async (): Promise<void> => {
  await built.app.close();
  built.db.close();
  process.exit(0);
};
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
