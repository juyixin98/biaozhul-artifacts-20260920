/**
 * Process entry point: load config (fail fast), open the store, wire the
 * diagnostic interface, and serve until SIGINT/SIGTERM.
 */

import { loadConfig } from './config';
import { createLogger } from './logger';
import { buildApp } from './server';
import { PatchService } from './service';
import { openStore } from './store';

async function main(): Promise<void> {
  const config = loadConfig();
  const logger = createLogger({ filePath: config.logFilePath, toStdout: config.logToStdout });
  const { store } = openStore(config.databasePath, config.prettyStorage);
  const service = new PatchService(store, logger);
  const app = buildApp({ service, store, logger });

  const close = async (signal: string): Promise<void> => {
    logger.log({ level: 'info', event: 'process.shutdown', signal });
    try {
      await app.close();
      store.close();
    } finally {
      process.exit(0);
    }
  };
  process.on('SIGINT', () => void close('SIGINT'));
  process.on('SIGTERM', () => void close('SIGTERM'));

  await app.listen({ port: config.httpPort, host: config.httpHost });
  logger.log({
    level: 'info',
    event: 'process.started',
    host: config.httpHost,
    port: config.httpPort,
    databasePath: config.databasePath,
  });
}

main().catch((err: unknown) => {
  process.stderr.write(`Fatal startup error: ${err instanceof Error ? err.stack ?? err.message : String(err)}\n`);
  process.exit(1);
});
