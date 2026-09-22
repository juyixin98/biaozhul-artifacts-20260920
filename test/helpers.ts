import { INestApplication, ValidationPipe } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import { Client } from 'pg';
import { AppModule } from '../src/app.module';
import { AppConfig } from '../src/config/app-config';

export interface TestAppOptions {
  idleMs?: number;
  sweepMs?: number;
}

export async function createTestApp(opts: TestAppOptions = {}): Promise<INestApplication> {
  const config = new AppConfig();
  // Per-instance override of the tunable timeouts.
  (config as { sessionIdleTimeoutMs: number }).sessionIdleTimeoutMs =
    opts.idleMs ?? 30 * 60_000;
  (config as { timeoutSweepMs: number }).timeoutSweepMs = opts.sweepMs ?? 60_000;

  const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
    .overrideProvider(AppConfig)
    .useValue(config)
    .compile();

  const app = moduleRef.createNestApplication();
  app.useGlobalPipes(new ValidationPipe({ whitelist: true, transform: true }));
  return app;
}

export async function listenEphemeral(app: INestApplication): Promise<number> {
  await app.listen(0, '127.0.0.1');
  const address = app.getHttpServer().address();
  if (address && typeof address === 'object') return address.port;
  throw new Error('Could not bind ephemeral port');
}

export async function truncateAll(): Promise<void> {
  const client = new Client({ connectionString: process.env.DATABASE_URL });
  await client.connect();
  await client.query(`
    TRUNCATE TABLE
      command_records, session_events, hand_raises, participants,
      sessions, presentation_versions, scenes, presentations
    RESTART IDENTITY CASCADE
  `);
  await client.end();
}

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

let counter = 0;
export function uniqueUser(prefix = 'user'): { userId: string; name: string } {
  counter += 1;
  const id = `${prefix}-${Date.now()}-${counter}`;
  return { userId: id, name: id };
}
