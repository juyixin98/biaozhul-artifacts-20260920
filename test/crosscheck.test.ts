/**
 * Independent cross-checks — the kernel is compared against TWO oracles that
 * are not the code under test:
 *
 *   1. test/fixtures/crosscheck-generated.json, produced by
 *      scripts/generate-crosscheck.py: an independent Python-stdlib RFC 6902
 *      implementation with concrete result (or concrete category + index).
 *   2. the third-party `fast-json-patch` npm package applied to the same
 *      scenarios: successful results must be byte-identical; failing
 *      scenarios must fail in both implementations.
 *
 * Assertions are on concrete outputs and failure categories — never on
 * "the interface was callable".
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import fjp from 'fast-json-patch';

import type { PatchOperation } from '../src/contract.ts';
import { applyPatch, type KernelFailure } from '../src/kernel.ts';

const here = dirname(fileURLToPath(import.meta.url));

const fixtureFile = JSON.parse(
  readFileSync(join(here, 'fixtures', 'crosscheck-generated.json'), 'utf8'),
) as {
  _meta: { generator: string; oracle: string; seed: number; count: number };
  scenarios: GeneratedScenario[];
};

interface GeneratedScenario {
  id: string;
  document: unknown;
  operations: Array<Record<string, unknown>>;
  expected:
    | { ok: true; result: unknown }
    | { ok: false; category: string; failedAtIndex: number; message: string };
}

describe('cross-check provenance', () => {
  it('fixture was produced by the independent Python generator, not the TS core', () => {
    assert.match(fixtureFile._meta.generator, /generate-crosscheck\.py$/);
    assert.match(fixtureFile._meta.oracle, /independent Python/);
    assert.equal(fixtureFile._meta.count, fixtureFile.scenarios.length);
    assert.ok(fixtureFile.scenarios.length >= 100);
  });
});

describe('kernel vs independent Python oracle', () => {
  let failureCases = 0;
  let successCases = 0;

  for (const scenario of fixtureFile.scenarios) {
    it(scenario.id, () => {
      // Feed the kernel directly: the generator only emits well-shaped ops,
      // and the cross-check target is the execution kernel, not the contract
      // layer (which has its own suite and rejects move-into-self earlier).
      const operations = scenario.operations as PatchOperation[];
      const outcome = applyPatch(scenario.document, operations);

      if (scenario.expected.ok) {
        successCases++;
        assert.equal(outcome.ok, true, describeFailure(outcome as KernelFailure));
        assert.deepEqual(
          (outcome as { result: unknown }).result,
          scenario.expected.result,
        );
      } else {
        failureCases++;
        assert.equal(outcome.ok, false, 'oracle expected failure, kernel succeeded');
        const failure = outcome as KernelFailure;
        assert.equal(failure.category, scenario.expected.category);
        assert.equal(failure.failedAtIndex, scenario.expected.failedAtIndex);
      }
    });
  }

  it('covers a meaningful mix of both outcomes', () => {
    assert.ok(successCases >= 50, `only ${successCases} success cases`);
    assert.ok(failureCases >= 20, `only ${failureCases} failure cases`);
  });
});

describe('kernel vs third-party fast-json-patch on identical scenarios', () => {
  for (const scenario of fixtureFile.scenarios) {
    it(`${scenario.id} (fjp agreement)`, () => {
      const operations = scenario.operations as PatchOperation[];
      const fjpDoc = structuredClone(scenario.document);

      let fjpFailed = false;
      let fjpResult: unknown;
      try {
        // applyPatch returns a result whose `.newDocument` is authoritative:
        // when the root is replaced the local variable still references the
        // old root, only newDocument points at the new one.
        // IMPORTANT: fast-json-patch MUTATES the operations array in place
        // (it writes intermediate documents back into root-level ops), so it
        // must receive a private copy — the shared fixture is read-only.
        const result = fjp.applyPatch(
          fjpDoc,
          structuredClone(operations) as fjp.Operation[],
          /* validate */ true,
        );
        fjpResult = result.newDocument;
      } catch {
        fjpFailed = true;
      }

      const outcome = applyPatch(scenario.document, operations);

      if (scenario.expected.ok) {
        assert.equal(outcome.ok, true);
        assert.equal(fjpFailed, false, 'fast-json-patch rejected a scenario both others accept');
        assert.deepEqual((outcome as { result: unknown }).result, fjpResult);
      } else {
        assert.equal(outcome.ok, false);

        const failingOp = operations[scenario.expected.failedAtIndex] as PatchOperation;
        const exactSelfMove =
          scenario.expected.category === 'MOVE_TARGET_DESCENDANT' &&
          failingOp.op === 'move' &&
          failingOp.from === failingOp.path;

        if (exactSelfMove) {
          // DOCUMENTED DIVERGENCE (RFC 6902 §4.4): the spec forbids moving into
          // a *proper* descendant; an exact from==path self-move is not spelled
          // out. fast-json-patch treats it as a no-op and is non-atomic across
          // the patch, while this service and the Python oracle reject it and
          // roll the ENTIRE patch back. Verify FJP's result is exactly the
          // state after the op prefix (i.e. the self-move changed nothing),
          // produced with a separate FJP run on a fresh clone.
          const prefixDoc = structuredClone(scenario.document);
          let prefixFailed = false;
          let prefixResult: unknown;
          try {
            prefixResult = fjp
              .applyPatch(
                prefixDoc,
                structuredClone(operations.slice(0, scenario.expected.failedAtIndex)) as fjp.Operation[],
                true,
              )
              .newDocument;
          } catch {
            prefixFailed = true;
          }
          assert.equal(prefixFailed, false, 'oracle prefix must apply cleanly in FJP');
          if (!fjpFailed) {
            assert.deepEqual(fjpResult, prefixResult);
          }
        } else {
          assert.equal(
            fjpFailed,
            true,
            `fast-json-patch accepted scenario ${scenario.id} which both other oracles reject`,
          );
        }
      }
    });
  }
});

function describeFailure(failure: KernelFailure): string {
  return `${failure.category} at op ${failure.failedAtIndex}: ${failure.message}`;
}
