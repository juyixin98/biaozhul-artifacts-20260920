/**
 * 进程入口：加载配置 → 打开 SQLite → （可选）种子 → 启动 HTTP 服务。
 */
import { loadConfig } from './config.js';
import { SAMPLE_OBJECTS } from '../fixtures/samples.js';
import { buildServer } from './server.js';
import { strongEtag } from './store/etag.js';
import { SqliteObjectStore } from './store/sqliteStore.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const store = new SqliteObjectStore(config.dbPath);

  // 库为空时自动播种合成夹具，保证首次启动即可用。
  if (store.listIds().length === 0) {
    for (const sample of SAMPLE_OBJECTS) {
      store.putObject({
        id: sample.id,
        content: sample.content,
        contentType: sample.contentType,
        lastModifiedMs: sample.lastModifiedMs,
        etag: strongEtag(sample.content),
      });
    }
    console.log(`[seed] 已写入 ${SAMPLE_OBJECTS.length} 个合成样例对象`);
  }

  const app = buildServer({
    store,
    maxRanges: config.maxRanges,
    maxResponseBytes: Number(config.maxResponseBytes),
    diagnosticsCapacity: config.diagnosticsCapacity,
  });

  const shutdown = async () => {
    await app.close();
    store.close();
    process.exit(0);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  await app.listen({ host: config.host, port: config.port });
  console.log(
    `[listen] http://${config.host}:${config.port}  ` +
      `(maxRanges=${config.maxRanges}, maxResponseBytes=${config.maxResponseBytes}, db=${config.dbPath})`,
  );
}

main().catch((err) => {
  console.error('[fatal]', err);
  process.exit(1);
});
