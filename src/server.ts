/**
 * Server entry point. Configuration comes from the environment (see
 * README §Configuration); invalid settings fail fast at startup.
 */
import { assemble } from './app.js';

const assembled = assemble();
const { app, store, config } = assembled;

const shutdown = async (signal: string): Promise<void> => {
  process.stdout.write(JSON.stringify({ event: 'shutdown', signal }) + '\n');
  try {
    await app.close();
    store.close();
  } finally {
    process.exit(0);
  }
};
process.on('SIGINT', () => void shutdown('SIGINT'));
process.on('SIGTERM', () => void shutdown('SIGTERM'));

app
  .listen({ port: config.port, host: config.host })
  .then(() => {
    process.stdout.write(
      JSON.stringify({
        event: 'server.listening',
        host: config.host,
        port: config.port,
        dbPath: config.dbPath
      }) + '\n'
    );
  })
  .catch((err: unknown) => {
    process.stderr.write(`Failed to start: ${err instanceof Error ? err.stack ?? err.message : String(err)}\n`);
    process.exit(1);
  });
