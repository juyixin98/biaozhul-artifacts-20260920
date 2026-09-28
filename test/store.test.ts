/**
 * State adapter tests: runs persist, findings/witnesses round-trip, and a
 * re-fetched run is correlated by the same request id.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { DiffStore } from '../src/state/diff-store.js';
import { DiffEngine } from '../src/kernel/diff-engine.js';
import { matrix } from './helpers/fixtures.js';

let store: DiffStore | null = null;

afterEach(() => {
  store?.close();
  store = null;
});

describe('SQLite diff store', () => {
  it('persists a run and reloads findings with witnesses', () => {
    store = DiffStore.memory();
    const { result } = new DiffEngine().diff(
      matrix.enumNarrowed().old,
      matrix.enumNarrowed().new,
      'run-enum-1',
    );
    store.saveRun(result);

    const reloaded = store.getRun('run-enum-1')!;
    expect(reloaded).toBeTruthy();
    expect(reloaded.compatible).toBe(0);
    expect(reloaded.breaking).toBe(1);
    expect(reloaded.findings).toHaveLength(1);
    expect(reloaded.findings[0]!.code).toBe('PARAM_ENUM_NARROWED');
    expect(reloaded.findings[0]!.witness).not.toBeNull();
    expect((reloaded.findings[0]!.witness!.example as { value: unknown }).value).toBe('sold');
  });

  it('lists runs newest first and returns null for unknown request id', () => {
    store = DiffStore.memory();
    const engine = new DiffEngine();
    store.saveRun(engine.diff(matrix.identical().old, matrix.identical().new, 'run-a').result);
    store.saveRun(engine.diff(matrix.enumNarrowed().old, matrix.enumNarrowed().new, 'run-b').result);

    const runs = store.listRuns();
    expect(runs.map((r) => r.request_id)).toEqual(['run-b', 'run-a']);
    expect(store.getRun('does-not-exist')).toBeNull();
  });

  it('persists uncertainties separately from hard findings', () => {
    store = DiffStore.memory();
    const cyclic = {
      openapi: '3.1.0',
      info: { title: 't', version: '1' },
      paths: {
        '/n': {
          post: {
            requestBody: {
              required: false,
              content: { 'application/json': { schema: { $ref: '#/components/schemas/Node' } } },
            },
            responses: { '200': { description: 'ok' } },
          },
        },
      },
      components: { schemas: { Node: { type: 'object', properties: { child: { $ref: '#/components/schemas/Node' } } } } },
    };
    const { result } = new DiffEngine().diff(cyclic, cyclic, 'run-cycle');
    store.saveRun(result);
    const reloaded = store.getRun('run-cycle')!;
    expect(reloaded.uncertainties.some((u) => u.code === 'REF_CYCLE')).toBe(true);
  });
});
