import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after } from 'node:test';
import { buildApp, type BuiltApp } from '../../src/app.js';
import type { AppConfig } from '../../src/config/index.js';

/** Response type of Fastify's in-process injector. */
export type InjectResponse = Awaited<ReturnType<BuiltApp['app']['inject']>>;

/**
 * Spin up the real adapter stack (Fastify + SQLite + diagnostics) against
 * an isolated temp directory. Used by every integration test so they never
 * touch data/ or a shared port.
 */
export interface Harness {
  built: BuiltApp;
  dir: string;
  logFile: string;
  get: (url: string, headers?: Record<string, string>) => Promise<InjectResponse>;
  post: (
    url: string,
    body: Buffer,
    headers?: Record<string, string>,
  ) => Promise<InjectResponse>;
  readJsonl: () => string[];
  cleanup: () => void;
}

export async function startHarness(
  overrides: Partial<AppConfig> = {},
): Promise<Harness> {
  const dir = mkdtempSync(join(tmpdir(), 'range-it-'));
  const logFile = join(dir, 'requests.jsonl');
  const built = await buildApp({
    databasePath: ':memory:',
    logPath: logFile,
    host: '127.0.0.1',
    port: 0,
    ...overrides,
  });

  after(async () => {
    await built.app.close();
    if (built.fileSink) await built.fileSink.flush();
    built.store.close();
    rmSync(dir, { recursive: true, force: true });
  });

  return {
    built,
    dir,
    logFile,
    get: (url, headers) => built.app.inject({ method: 'GET', url, headers }),
    post: (url, body, headers) =>
      built.app.inject({
        method: 'POST',
        url,
        payload: body,
        headers: { 'content-type': 'application/octet-stream', ...headers },
      }),
    readJsonl: () => {
      try {
        return readFileSync(logFile, 'utf8')
          .split('\n')
          .filter((line) => line.trim() !== '');
      } catch {
        return [];
      }
    },
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

/** Header lookup that tolerates Fastify/light-my-request casing. */
export function header(res: InjectResponse, name: string): string | undefined {
  const found = Object.entries(res.headers).find(
    ([k]) => k.toLowerCase() === name.toLowerCase(),
  );
  return found?.[1] as string | undefined;
}
