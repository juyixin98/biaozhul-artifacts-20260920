import { execSync } from 'child_process';
import * as net from 'net';

// Boots a disposable Postgres in Docker for the whole test run, unless
// DATABASE_URL already points at one.
export default async function globalSetup() {
  if (process.env.DATABASE_URL) {
    return;
  }
  const name = `stagevault-test-${process.pid}-${Date.now()}`;
  const port = await freePort();
  execSync(
    [
      'docker run -d --rm',
      `--name ${name}`,
      `-e POSTGRES_USER=stagevault`,
      `-e POSTGRES_PASSWORD=stagevault`,
      `-e POSTGRES_DB=stagevault`,
      `-p 127.0.0.1:${port}:5432`,
      'postgres:16-alpine',
    ].join(' '),
    { stdio: 'ignore' },
  );
  process.env.__TEST_PG_CONTAINER = name;
  process.env.DATABASE_URL = `postgres://stagevault:stagevault@localhost:${port}/stagevault`;

  // Wait for readiness.
  const { Client } = await import('pg');
  const deadline = Date.now() + 60_000;
  for (;;) {
    const client = new Client({ connectionString: process.env.DATABASE_URL });
    try {
      await client.connect();
      await client.query('SELECT 1');
      break;
    } catch {
      if (Date.now() > deadline) throw new Error('Test Postgres did not become ready');
      await new Promise((r) => setTimeout(r, 500));
    } finally {
      await client.end().catch(() => undefined);
    }
  }
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address();
      srv.close(() => resolve(typeof addr === 'object' && addr ? addr.port : 0));
    });
  });
}
