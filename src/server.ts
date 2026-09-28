/**
 * Runnable service entrypoint: `npm run dev` (tsx) or `npm start` (built).
 */
import { buildApp } from './http/app.js';
import { config } from './config/index.js';

async function main(): Promise<void> {
  const { app, store } = await buildApp();
  try {
    await app.listen({ port: config.port, host: config.host });
    app.log.info(
      {
        port: config.port,
        host: config.host,
        db: config.dbPath,
        log: config.logPath,
      },
      'composite partial-aggregation service listening',
    );
  } catch (err) {
    store.close();
    throw err;
  }

  const shutdown = (): void => {
    app
      .close()
      .then(() => store.close())
      .then(() => process.exit(0))
      .catch(() => process.exit(1));
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((err) => {
  console.error('fatal startup error', err);
  process.exit(1);
});
