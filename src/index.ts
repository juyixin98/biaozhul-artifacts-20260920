import { buildApp } from './app.js';

/**
 * Server entry point. Configuration is 12-factor style (see README):
 *   PORT, HOST, DB_PATH, DIAGNOSTICS_LOG, LOG_BODIES,
 *   RANGE_MAX_SPECS, RANGE_MAX_RESPONSE_BYTES, RANGE_MERGE_GAP
 */
async function main(): Promise<void> {
  const { app, store, fileSink, config } = await buildApp();

  const shutdown = async (signal: string): Promise<void> => {
    console.log(`Received ${signal}, draining...`);
    await app.close();
    if (fileSink) await fileSink.flush();
    store.close();
    process.exit(0);
  };
  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));

  await app.listen({ host: config.host, port: config.port });
  console.log(
    `Range backend on http://${config.host}:${config.port} ` +
      `(db=${config.databasePath}, maxSpecs=${config.limits.maxSpecs}, ` +
      `maxResponseBytes=${config.limits.maxResponseBytes}, mergeGap=${config.limits.mergeGap})`,
  );
  console.log('Endpoints: GET /objects/:id, GET /diagnostics, POST /objects/:id');
}

main().catch((err: unknown) => {
  console.error('Fatal startup error:', err);
  process.exit(1);
});
