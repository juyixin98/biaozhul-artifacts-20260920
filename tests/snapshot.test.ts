import { describe, expect, it } from 'vitest';
import { createHarness, fieldByPath, nodeById, recordVerdict } from './helpers.js';
import { snapshotToken } from '../src/fixtures/scenario.js';

describe('snapshot consistency', () => {
  it('marks a source that cannot snapshot as a best-effort consistency limitation', async () => {
    const runId = 'snapshot-unsupported-best-effort';
    // recommendations defaults to supportsSnapshot=false, best-effort node.
    const h = createHarness({}, runId);
    const response = await h.run({ runId, snapshotToken: snapshotToken('v1') });

    expect(response.outcome).toBe('complete');

    const recNode = nodeById(response, 'recommendations');
    expect(recNode.snapshotTokenUsed).toBe(snapshotToken('v1'));
    expect(recNode.snapshotHonoured).toBe(false);

    const note = response.consistency.find((c) => c.nodeId === 'recommendations');
    expect(note).toMatchObject({
      kind: 'snapshot-unsupported',
      source: 'recommendations-fixture',
    });

    // The field still resolves (limited consistency is not a failure).
    expect(fieldByPath(response, 'recommendation').status).toBe('present');

    recordVerdict(
      runId,
      'PASS',
      'same token delivered; non-snapshot-capable best-effort source flagged snapshot-unsupported, still served',
    );
  });

  it('detects source version drift under a token and records inconsistent versions', async () => {
    const runId = 'snapshot-version-drift';
    const h = createHarness({ pricingVersion: 'v2' }, runId);
    const response = await h.run({ runId, snapshotToken: snapshotToken('v1') });

    expect(response.outcome).toBe('complete'); // best-effort: partial consistency
    expect(response.dataVersion).toEqual({
      requested: 'v1',
      observed: ['v1', 'v2'],
      consistent: false,
    });

    const pricingNote = response.consistency.find(
      (c) => c.nodeId === 'pricing' && c.kind === 'snapshot-mismatch',
    );
    expect(pricingNote).toBeDefined();
    expect(pricingNote!.detail).toContain('v2');
    expect(pricingNote!.detail).toContain('v1');

    recordVerdict(
      runId,
      'PASS',
      'pricing served v2 under v1 token -> snapshot-mismatch note and observed=[v1,v2] inconsistent',
    );
  });

  it('hard-fails an exact-snapshot node whose source cannot snapshot (STATE_CONFLICT)', async () => {
    const runId = 'snapshot-exact-unsupported-conflict';
    const h = createHarness(
      { inventorySnapshotExact: true, inventorySupportsSnapshot: false },
      runId,
    );
    const response = await h.run({ runId, snapshotToken: snapshotToken('v1') });

    expect(response.outcome).toBe('partial');
    const inventoryNode = nodeById(response, 'inventory');
    expect(inventoryNode.status).toBe('failed');
    expect(inventoryNode.failure).toMatchObject({
      category: 'STATE_CONFLICT',
      code: 'SNAPSHOT_NOT_SUPPORTED',
    });
    expect(inventoryNode.failure!.category).toBe('STATE_CONFLICT');

    // Consistency note for downstream that consumed the conflicted node's data.
    expect(
      response.consistency.some(
        (c) => c.kind === 'snapshot-mismatch' && c.nodeId === 'recommendations',
      ),
    ).toBe(true);

    // Dependent required fields are missing with the conflict reason.
    expect(fieldByPath(response, 'product.name').reasons[0]?.category).toBe('STATE_CONFLICT');

    recordVerdict(
      runId,
      'PASS',
      'exact-snapshot requirement against incapable source -> STATE_CONFLICT; downstream annotated',
    );
  });

  it('hard-fails an exact-snapshot node on version drift (STATE_CONFLICT)', async () => {
    const runId = 'snapshot-exact-drift-conflict';
    const h = createHarness(
      { inventorySnapshotExact: true, inventoryVersion: 'v2' },
      runId,
    );
    const response = await h.run({ runId, snapshotToken: snapshotToken('v1') });

    const inventoryNode = nodeById(response, 'inventory');
    expect(inventoryNode.status).toBe('failed');
    expect(inventoryNode.failure).toMatchObject({
      category: 'STATE_CONFLICT',
      code: 'SNAPSHOT_VERSION_DRIFT',
    });
    expect(inventoryNode.failure!.message).toContain('v2');
    expect(inventoryNode.failure!.message).toContain('v1');

    recordVerdict(
      runId,
      'PASS',
      'exact-snapshot node serving v2 under v1 token -> SNAPSHOT_VERSION_DRIFT STATE_CONFLICT',
    );
  });

  it('annotates all nodes when no snapshot token is requested', async () => {
    const runId = 'snapshot-not-requested';
    const h = createHarness({}, runId);
    const response = await h.run({ runId, snapshotToken: null });

    const succeeded = response.nodeResults.filter((n) => n.status === 'succeeded');
    expect(response.consistency).toHaveLength(succeeded.length);
    expect(response.consistency.every((c) => c.kind === 'snapshot-not-requested')).toBe(true);
    expect(response.dataVersion.requested).toBeNull();

    recordVerdict(runId, 'PASS', 'no token -> every succeeded node annotated snapshot-not-requested');
  });
});
