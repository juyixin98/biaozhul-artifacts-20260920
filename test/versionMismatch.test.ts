/**
 * Source version inconsistency under one snapshot token.
 *
 * Every snapshot-capable source reports the epoch version it actually served.
 * The healthy sources agree; the pricing source in this scenario serves a newer
 * hot version. The engine must:
 *  - still complete (version skew is reported, not thrown),
 *  - emit exactly one VERSION_MISMATCH limitation listing the divergent sources
 *    and the concrete versions observed,
 *  - keep the snapshot-incapable legacy source in its own limitation bucket.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { runComposite } from './helpers/harness.js';

describe('source version inconsistency', () => {
  it('reports VERSION_MISMATCH with the divergent source and served versions', async () => {
    const { result, node } = await runComposite({
      baseLatencyMs: 0,
      pricingDataVersion: 'pricing-hot-2026-09-27',
    });

    assert.equal(result.status, 'complete');
    const mismatch = result.limitations.find((l) => l.type === 'VERSION_MISMATCH');
    assert.ok(mismatch, 'serving different versions under one token is a limitation');
    assert.ok(mismatch!.sources.includes('pricing'));
    assert.ok(mismatch!.sources.includes('catalog'));
    assert.equal(mismatch!.versions!.pricing, 'pricing-hot-2026-09-27');
    assert.equal(mismatch!.versions!.catalog, 'catalog-epoch-2026-09-01');
    // The node record carries the per-call version that fed the comparison.
    assert.equal(node('quote').sourceVersion, 'pricing-hot-2026-09-27');
    assert.equal(node('product').sourceVersion, 'catalog-epoch-2026-09-01');
  });

  it('reports no VERSION_MISMATCH when all snapshot-capable sources agree', async () => {
    const { result } = await runComposite({ baseLatencyMs: 0 });
    const mismatch = result.limitations.find((l) => l.type === 'VERSION_MISMATCH');
    assert.equal(mismatch, undefined);
    // Snapshot-incapable legacy source is still reported separately.
    assert.ok(result.limitations.some((l) => l.type === 'SNAPSHOT_UNSUPPORTED'));
  });

  it('does not compare versions from the non-pinned legacy source', async () => {
    // Even though contacts reports a different string
    // ('unstamped-live-read'), it must not drive VERSION_MISMATCH.
    const { result } = await runComposite({ baseLatencyMs: 0 });
    const mismatch = result.limitations.find((l) => l.type === 'VERSION_MISMATCH');
    assert.equal(mismatch, undefined);
    const snapshot = result.limitations.find((l) => l.type === 'SNAPSHOT_UNSUPPORTED')!;
    assert.deepEqual(snapshot.sources, ['contacts']);
  });

  it('passes the same snapshot token while sources disagree on version', async () => {
    const { registry } = await runComposite({
      baseLatencyMs: 0,
      pricingDataVersion: 'pricing-hot-2026-09-27',
      snapshotToken: 'snap-skew-1',
    });
    const tokens = new Set(registry.all().map((i) => i.contextSnapshot.snapshotToken));
    assert.deepEqual([...tokens], ['snap-skew-1']);
  });
});
