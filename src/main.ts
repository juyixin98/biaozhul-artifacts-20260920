import { buildApp } from './app.js';
import { loadConfig } from './config.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const app = buildApp(config);

  const shutdown = async (signal: string): Promise<void> => {
    app.logger.record({
      kind: 'shutdown',
      decision: 'indeterminate',
      reason: `received-${signal}`,
      detail: { liveTasks: app.tasks.liveCount() },
    });
    // Give detached tasks a bounded window to finish so their outcomes persist.
    await app.tasks.drain(5_000);
    await app.server.close();
    app.state.close();
    app.domain.close();
    process.exit(0);
  };

  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));

  try {
    await app.server.listen({ host: config.host, port: config.port });
    app.logger.record({
      kind: 'server-started',
      decision: 'accepted',
      reason: 'listening',
      detail: { host: config.host, port: config.port, dbPath: config.dbPath },
    });
  } catch (err) {
    process.stderr.write(`Failed to start: ${(err as Error).message}\n`);
    process.exit(1);
  }
}

void main();
