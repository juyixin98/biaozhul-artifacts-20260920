/**
 * Integration support: build the real app over a throwaway SQLite file and
 * replay fixture steps through Fastify's inject() transport (full HTTP
 * parsing, routing, content negotiation and error mapping, no network port).
 */
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { FastifyInstance } from 'fastify';
import { assemble } from '../../src/app.js';
import type { AssembledApp } from '../../src/app.js';
import { AppConfig } from '../../src/config.js';

export interface TempApp extends AssembledApp {
  readonly dir: string;
  cleanup(): void;
}

export function buildTempApp(retainLogs = false): TempApp {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'rv-api-'));
  const config: AppConfig = {
    port: 0,
    host: '127.0.0.1',
    dbPath: path.join(dir, 'test.db'),
    logLevel: 'fatal'
  };
  const sink = openNullSink();
  const assembled = assemble(config, sink, retainLogs);
  return { ...assembled, dir, cleanup: () => cleanup(assembled, dir) };
}

function openNullSink(): NodeJS.WritableStream {
  // A tiny no-op writable avoids polluting test output while retaining logs
  // in the logger's buffer when retainLogs is true.
  return { write: () => true, on: () => ({}), once: () => ({}), emit: () => false } as unknown as NodeJS.WritableStream;
}

function cleanup(assembled: AssembledApp, dir: string): void {
  assembled.store.close();
  rmSync(dir, { recursive: true, force: true });
}

export interface ReplayStep {
  method: 'GET' | 'HEAD' | 'PUT' | 'PATCH' | 'DELETE';
  path: string;
  body?: unknown;
  headers: Record<string, string>;
  expectStatus?: number;
  expectEtag?: string;
  expectVersion?: number;
  expectErrorCode?: string;
  expectBodyField?: [string, unknown];
  comment?: string;
}

export interface ReplayResult {
  status: number;
  headers: Record<string, string | undefined>;
  bodyJson: unknown;
  rawBody: string;
}

export async function injectStep(app: FastifyInstance, step: ReplayStep): Promise<ReplayResult> {
  const payload = step.body === undefined ? undefined : JSON.stringify(step.body);
  const response = await app.inject({
    method: step.method,
    url: step.path,
    headers: {
      ...(payload !== undefined ? { 'content-type': 'application/json' } : {}),
      ...step.headers
    },
    payload
  });
  const lowerHeaders: Record<string, string | undefined> = {};
  for (const [k, v] of Object.entries(response.headers)) lowerHeaders[k.toLowerCase()] = Array.isArray(v) ? v[0] : String(v);
  let bodyJson: unknown = undefined;
  if (response.body) {
    try {
      bodyJson = JSON.parse(response.body);
    } catch {
      bodyJson = undefined;
    }
  }
  return { status: response.statusCode, headers: lowerHeaders, bodyJson, rawBody: response.body };
}

/** Current ETag header for a resource path, captured via an unconditional GET. */
export async function currentEtag(app: FastifyInstance, resourcePath: string, weak = false): Promise<string> {
  const url = weak ? `${resourcePath}?weak=1` : resourcePath;
  const res = await injectStep(app, { method: 'GET', path: url, headers: {} });
  if (!res.headers.etag) throw new Error(`no etag at ${url}`);
  return res.headers.etag;
}
