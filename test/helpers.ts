import { INestApplication, ValidationPipe } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import { WsAdapter } from '@nestjs/platform-ws';
import { Client } from 'pg';
import { DataSource } from 'typeorm';
import WebSocket from 'ws';
import { AppModule } from '../src/app.module';

export interface TestContext {
  app: INestApplication;
  dataSource: DataSource;
  baseUrl: string;
  port: number;
}

/** Create the test database if it does not exist yet. */
async function ensureDatabase() {
  const url = new URL(process.env.DATABASE_URL as string);
  const dbName = url.pathname.replace(/^\//, '');
  const admin = new Client({
    host: url.hostname,
    port: Number(url.port || 5432),
    user: decodeURIComponent(url.username),
    password: decodeURIComponent(url.password),
    database: 'postgres',
  });
  await admin.connect();
  const { rowCount } = await admin.query(
    'SELECT 1 FROM pg_database WHERE datname = $1',
    [dbName],
  );
  if (rowCount === 0) {
    await admin.query(`CREATE DATABASE "${dbName}"`);
  }
  await admin.end();
}

export async function createTestApp(): Promise<TestContext> {
  await ensureDatabase();
  const moduleRef = await Test.createTestingModule({
    imports: [AppModule],
  }).compile();
  const app = moduleRef.createNestApplication();
  app.useWebSocketAdapter(new WsAdapter(app));
  app.useGlobalPipes(new ValidationPipe({ whitelist: true, transform: true }));
  await app.init();
  const dataSource = moduleRef.get(DataSource);
  await dataSource.runMigrations();
  await app.listen(0);
  const port = (app.getHttpServer().address() as any).port;
  return { app, dataSource, port, baseUrl: `http://127.0.0.1:${port}` };
}

export async function cleanDatabase(dataSource: DataSource) {
  await dataSource.query(
    'TRUNCATE hand_raises, command_receipts, session_events, participants, sessions, presentation_versions, presentations CASCADE',
  );
}

export interface ApiResponse {
  status: number;
  body: any;
}

export function makeApi(baseUrl: string) {
  return async function api(
    method: string,
    path: string,
    opts: { token?: string; body?: unknown } = {},
  ): Promise<ApiResponse> {
    const res = await fetch(baseUrl + path, {
      method,
      headers: {
        'content-type': 'application/json',
        ...(opts.token ? { 'x-session-token': opts.token } : {}),
      },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const body = await res.json().catch(() => null);
    return { status: res.status, body };
  };
}

/** Create a published presentation version + a session bound to it. */
export async function setupSession(
  api: ReturnType<typeof makeApi>,
  sceneCount = 3,
) {
  const presentation = (
    await api('POST', '/presentations', { body: { title: 'Demo' } })
  ).body;
  const scenes = Array.from({ length: sceneCount }, (_, i) => ({
    title: `Scene ${i}`,
  }));
  const version = (
    await api('POST', `/presentations/${presentation.id}/versions`, {
      body: { scenes },
    })
  ).body;
  await api('POST', `/presentation-versions/${version.id}/publish`);
  const session = (
    await api('POST', '/sessions', {
      body: { presentationVersionId: version.id, hostName: 'Host' },
    })
  ).body;
  return { presentation, version, session };
}

/** Tiny WebSocket test client with ordered message collection. */
export class TestWsClient {
  private ws: WebSocket;
  messages: any[] = [];
  private waiters: Array<{
    pred: (m: any) => boolean;
    resolve: (m: any) => void;
    reject: (e: Error) => void;
    timer: NodeJS.Timeout;
  }> = [];

  async connect(port: number): Promise<void> {
    this.ws = new WebSocket(`ws://127.0.0.1:${port}/ws`);
    this.ws.on('message', (raw) => {
      const msg = JSON.parse(raw.toString());
      this.messages.push(msg);
      this.waiters = this.waiters.filter((w) => {
        if (w.pred(msg)) {
          clearTimeout(w.timer);
          w.resolve(msg);
          return false;
        }
        return true;
      });
    });
    await new Promise<void>((resolve, reject) => {
      this.ws.once('open', () => resolve());
      this.ws.once('error', reject);
    });
  }

  send(msg: unknown) {
    this.ws.send(JSON.stringify(msg));
  }

  waitFor(pred: (m: any) => boolean, timeoutMs = 5000): Promise<any> {
    const existing = this.messages.find(pred);
    if (existing) return Promise.resolve(existing);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error('timed out waiting for ws message')),
        timeoutMs,
      );
      this.waiters.push({ pred, resolve, reject, timer });
    });
  }

  close() {
    try {
      this.ws.close();
    } catch {
      /* already closed */
    }
  }
}
