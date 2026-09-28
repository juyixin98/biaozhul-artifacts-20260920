/**
 * 进程入口：加载配置 -> 打开/迁移 SQLite -> 确保已播种 -> 启动 HTTP。
 */
import { buildApp } from './app.js';
import { loadConfig } from './config.js';
import { openDatabase } from './data/db.js';
import { isSeeded, seedDatabase } from './data/fixtures.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const db = openDatabase(config.dbPath);
  if (!isSeeded(db)) {
    const counts = seedDatabase(db);
    // 启动阶段的一次性运维信息，走 fastify logger 而非裸 console
    process.stderr.write(
      `seeded local database: ${counts.users} users, ${counts.posts} posts, ${counts.comments} comments\n`,
    );
  }

  const { app } = buildApp({
    db,
    ringBufferSize: config.ringBufferSize,
    logFile: config.logFile,
  });

  try {
    await app.listen({ host: config.host, port: config.port });
  } catch (err) {
    app.log.error(err, 'failed to start HTTP server');
    process.exitCode = 1;
  }

  const shutdown = async (signal: string): Promise<void> => {
    app.log.info({ signal }, 'shutting down');
    try {
      await app.close();
      db.close();
    } finally {
      process.exit(0);
    }
  };
  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));
}

void main();
