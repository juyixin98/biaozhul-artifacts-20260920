import { mkdirSync } from 'node:fs';
import { SqliteObjectStore } from '../src/state/store.js';

/**
 * Deterministic local fixtures — no network, no real business data.
 * Run with: npm run seed
 */

function patternBytes(size: number, salt: number): Buffer {
  const buf = Buffer.alloc(size);
  for (let i = 0; i < size; i++) {
    // Mix position and salt so equal-length objects differ byte-for-byte.
    buf[i] = (i * 31 + salt) & 0xff;
  }
  return buf;
}

async function main(): Promise<void> {
  const dbPath = process.env.DB_PATH ?? 'data/objects.db';
  mkdirSync('data', { recursive: true });
  const store = new SqliteObjectStore(dbPath);

  const seeds: Array<{ id: string; contentType: string; data: Buffer; createdAt: Date }> = [
    {
      id: 'empty',
      contentType: 'application/octet-stream',
      data: Buffer.alloc(0),
      createdAt: new Date('2026-01-01T00:00:00.000Z'),
    },
    {
      id: 'hello',
      contentType: 'text/plain; charset=utf-8',
      data: Buffer.from('Hello, Range world!', 'utf8'),
      createdAt: new Date('2026-01-02T00:00:00.000Z'),
    },
    {
      id: 'alpha',
      contentType: 'text/plain; charset=utf-8',
      data: Buffer.from('abcdefghijklmnopqrstuvwxyz', 'utf8'),
      createdAt: new Date('2026-01-03T00:00:00.000Z'),
    },
    {
      id: 'binary-256',
      contentType: 'application/octet-stream',
      data: Buffer.from(Array.from({ length: 256 }, (_, i) => i)),
      createdAt: new Date('2026-01-04T00:00:00.000Z'),
    },
    {
      id: 'blob-120k',
      contentType: 'application/octet-stream',
      data: patternBytes(120 * 1024, 7),
      createdAt: new Date('2026-01-05T00:00:00.000Z'),
    },
  ];

  for (const seed of seeds) {
    const existed = store.getMeta(seed.id);
    if (existed) {
      console.log(`skip ${seed.id} (already exists; objects are immutable)`);
      continue;
    }
    const meta = store.put(seed);
    console.log(`seeded ${seed.id}: ${meta.size} bytes, etag=${meta.etag.slice(0, 19)}…`);
  }

  store.close();
  console.log(`Done. Database at ${dbPath}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
