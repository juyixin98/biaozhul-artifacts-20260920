import { describe, expect, it } from 'vitest';
import { ResourceKernel } from '../../src/core/kernel.js';
import { InMemoryResourceStore } from '../../src/state/memory-store.js';
import { PreconditionFailedError } from '../../src/contract/errors.js';
import { Clock } from '../../src/core/kernel.js';
import { independentETag } from '../helpers/oracle.js';

function makeKernel(clock?: Clock): { kernel: ResourceKernel; store: InMemoryResourceStore } {
  const store = new InMemoryResourceStore();
  return { kernel: new ResourceKernel(store, clock), store };
}

const STEP_CLOCK: Clock = (() => {
  let t = Date.parse('2026-01-15T09:00:00Z');
  return { now: () => (t += 1000) };
})();

describe('ResourceKernel against the independent in-memory adapter', () => {
  it('PUT create returns 201 and the response ETag matches the independent oracle', () => {
    const { kernel } = makeKernel(STEP_CLOCK);
    const body = { name: 'widget', count: 1, tags: ['x'] };
    const outcome = kernel.put('r', body, {});
    expect(outcome.status).toBe(201);
    expect(outcome.representation?.etagHeader).toBe(independentETag(1, body));
    expect(outcome.representation?.snapshot.body).toEqual(body);
  });

  it('ETag and body in the outcome come from the same committed snapshot', () => {
    const { kernel } = makeKernel(STEP_CLOCK);
    const body = { a: 1 };
    const outcome = kernel.put('r', body, {});
    const persisted = kernel.get('r', {})!;
    expect(outcome.representation?.snapshot.version).toBe(persisted.representation.snapshot.version);
    expect(outcome.representation?.etagHeader).toBe(persisted.representation.etagHeader);
  });

  it('GET 304 still carries the current snapshot validators', () => {
    const { kernel } = makeKernel(STEP_CLOCK);
    kernel.put('r', { v: 1 }, {});
    const current = kernel.get('r', {})!;
    const etag = current.representation.etagHeader;
    const reread = kernel.get('r', { ifNoneMatch: etag });
    expect(reread?.kind).toBe('not-modified');
    expect(reread?.representation.etagHeader).toBe(etag);
  });

  it('simulated two-client race: the stale writer loses and no update is lost', () => {
    const { kernel, store } = makeKernel(STEP_CLOCK);
    kernel.put('doc', { text: 'v0' }, {});
    const clientAView = kernel.get('doc', {})!.representation.etagHeader;

    // Client A commits first.
    const a = kernel.put('doc', { text: 'A' }, { ifMatch: clientAView });
    expect(a.status).toBe(200);

    // Client B still holds the v1 validator and now attempts its write.
    try {
      kernel.put('doc', { text: 'B' }, { ifMatch: clientAView });
      throw new Error('expected 412');
    } catch (err) {
      expect(err).toBeInstanceOf(PreconditionFailedError);
      expect((err as PreconditionFailedError).code).toBe('PRECONDITION_IF_MATCH_FAILED');
    }

    // B refreshes, retries against the current version, and wins cleanly.
    const fresh = kernel.get('doc', {})!.representation.etagHeader;
    const bRetry = kernel.put('doc', { text: 'B' }, { ifMatch: fresh });
    expect(bRetry.status).toBe(200);

    const final = kernel.get('doc', {})!.representation;
    expect(final.snapshot.version).toBe(3);
    expect(final.snapshot.body).toEqual({ text: 'B' });
    expect(store.listVersions('doc').map((v) => v.version)).toEqual([1, 2, 3]);
  });

  it('PATCH requires an existing resource (404, not an implicit create)', () => {
    const { kernel } = makeKernel(STEP_CLOCK);
    expect(() => kernel.patch('ghost', { a: 1 }, {})).toThrowError(/does not exist|absent/);
  });

  it('delete appends a tombstone; recreation continues version numbering', () => {
    const { kernel, store } = makeKernel(STEP_CLOCK);
    kernel.put('r', { v: 1 }, {});
    expect(kernel.remove('r', {}).status).toBe(204);
    expect(kernel.get('r', {})).toBeNull();
    expect(store.listVersions('r')).toHaveLength(2);

    const recreated = kernel.put('r', { v: 2 }, { ifNoneMatch: '*' });
    expect(recreated.status).toBe(201);
    expect(recreated.representation?.snapshot.version).toBe(3);
    expect(store.listVersions('r').map((v) => v.version)).toEqual([1, 2, 3]);
  });

  it('If-Match * permits replacement of an existing resource', () => {
    const { kernel } = makeKernel(STEP_CLOCK);
    kernel.put('r', { v: 1 }, {});
    const outcome = kernel.put('r', { v: 2 }, { ifMatch: '*' });
    expect(outcome.status).toBe(200);
  });
});
