/**
 * Seed the local database with synthetic fixtures.
 *
 * All content is invented locally — no external accounts or business data.
 * A fixed, monotonic clock gives every version a reproducible Last-Modified,
 * so the request samples in the README replay against known validators.
 * Seeding is a no-op when the database already holds resources.
 */
import { loadConfig } from '../src/config.js';
import { ResourceKernel, Clock } from '../src/core/kernel.js';
import { SqliteResourceStore } from '../src/state/sqlite-store.js';

const BASE_TS = Date.parse('2026-01-15T09:00:00.000Z');

function fixedClock(start: number, stepMs: number): Clock {
  let t = start;
  return { now: () => (t += stepMs) - stepMs };
}

const store = new SqliteResourceStore(loadConfig().dbPath);
try {
  if (store.countResources() > 0) {
    process.stdout.write(`Seed skipped: database already contains ${store.countResources()} resource(s)\n`);
  } else {
    const clock = fixedClock(BASE_TS, 60_000);
    const kernel = new ResourceKernel(store, clock);

    kernel.put('invoice-1001', {
      customer: 'Synthetic Buyer Ltd.',
      currency: 'USD',
      lines: [{ sku: 'WIDGET-A', qty: 2, unitPrice: 9.5 }],
      status: 'draft'
    }, {});
    kernel.put('invoice-1001', {
      customer: 'Synthetic Buyer Ltd.',
      currency: 'USD',
      lines: [
        { sku: 'WIDGET-A', qty: 2, unitPrice: 9.5 },
        { sku: 'GADGET-B', qty: 1, unitPrice: 18.0 }
      ],
      status: 'issued'
    }, {});
    kernel.put('profile', { owner: 'local-tester', theme: 'dark', notifications: true }, {});

    process.stdout.write(`Seeded ${store.countResources()} resources with fixed timestamps from ${new Date(BASE_TS).toISOString()}\n`);
  }
} finally {
  store.close();
}
