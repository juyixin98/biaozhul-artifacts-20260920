/**
 * Service entrypoint.
 *
 * Configuration is environment driven:
 *   PORT           listen port            (default 3000)
 *   HOST           listen host            (default 127.0.0.1)
 *   DB_PATH        SQLite file            (default .data/app.db)
 *   TEMP_DIR       root for per-request   (default .data/tmp)
 *                  temp directories
 *   MAX_PART_BYTES, MAX_TOTAL_BYTES, MAX_FIELD_BYTES, MAX_HEADER_BYTES,
 *   MAX_PARTS      quota overrides in bytes/count
 */

import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { buildApp } from './server/app.js';
import { SubmissionRepository } from './storage/repository.js';
import { createDefaultConfig, type MultipartConfig } from './protocol/config.js';

function readInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer, got "${raw}"`);
  }
  return value;
}

async function main(): Promise<void> {
  const port = readInt('PORT', 3000);
  const host = process.env.HOST ?? '127.0.0.1';
  const dataDir = path.resolve(process.env.DATA_DIR ?? '.data');
  const dbPath = process.env.DB_PATH ?? path.join(dataDir, 'app.db');
  const tempDir = process.env.TEMP_DIR ?? path.join(dataDir, 'tmp');
  const logPath = process.env.RUN_LOG ?? path.join(dataDir, 'runs.jsonl');

  await mkdir(dataDir, { recursive: true });
  await mkdir(tempDir, { recursive: true });

  const base = createDefaultConfig({ tempDir });
  const config: MultipartConfig = {
    ...base,
    limits: {
      ...base.limits,
      maxPartBytes: readInt('MAX_PART_BYTES', base.limits.maxPartBytes),
      maxFieldBytes: readInt('MAX_FIELD_BYTES', base.limits.maxFieldBytes),
      maxTotalBytes: readInt('MAX_TOTAL_BYTES', base.limits.maxTotalBytes),
      maxHeaderBytes: readInt('MAX_HEADER_BYTES', base.limits.maxHeaderBytes),
      maxParts: readInt('MAX_PARTS', base.limits.maxParts),
    },
  };

  const repo = new SubmissionRepository(dbPath);
  const app = await buildApp({ config, repo, logPath });

  const shutdown = async (): Promise<void> => {
    await app.close();
    repo.close();
  };
  process.once('SIGINT', () => void shutdown().then(() => process.exit(0)));
  process.once('SIGTERM', () => void shutdown().then(() => process.exit(0)));

  await app.listen({ port, host });
  app.log.info?.(`multipart service listening on http://${host}:${port}`);
}

main().catch((err: unknown) => {
  process.stderr.write(`fatal: ${(err as Error).message}\n`);
  process.exit(1);
});
