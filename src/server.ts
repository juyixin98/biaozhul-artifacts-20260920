/**
 * Process entry point: wires config, SQLite store, run logger and the Fastify
 * application, and performs graceful shutdown.
 */

import { buildApp } from './app/app.js';
import { loadConfig } from './config.js';
import { RunLogger } from './diagnostics/run-logger.js';
import { SubmissionStore } from './storage/submission-store.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const store = new SubmissionStore(config.dbPath);
  const logger = new RunLogger(config.runLogPath);
  const app = buildApp({ config, store, logger });

  const shutdown = (signal: string): void => {
    app.log?.info?.(`received ${signal}, shutting down`);
    void app
      .close()
      .then(() => store.close())
      .then(() => process.exit(0))
      .catch(() => process.exit(1));
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  try {
    await app.listen({ port: config.port, host: config.host });
    // eslint-disable-next-line no-console
    console.log(
      JSON.stringify({
        msg: 'multipart receiver listening',
        host: config.host,
        port: config.port,
        dbPath: config.dbPath,
        filesDir: config.filesDir,
        tmpDir: config.tmpDir,
        runLogPath: config.runLogPath
      })
    );
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error('failed to start server:', err);
    process.exit(1);
  }
}

void main();
