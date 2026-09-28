/**
 * Field-level semantics on a minimal in-memory DAG.
 *
 * Distinguishes the four ways an output field can end up:
 *   present, present-via-default, optional missing (legal absence), failed
 * (required failure or its source errored).
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { ExecutionKernel } from '../src/kernel/executor.js';
import { RunLog } from '../src/observability/runLog.js';
import { FixtureSource } from '../src/sources/fixtures/fixtureSource.js';
import { SourceRegistry } from '../src/sources/registry.js';
import { FaultPlan } from '../src/sources/fixtures/faults.js';
import type { CompositeContract } from '../src/contract/types.js';
import type { CompositeResult } from '../src/kernel/types.js';

interface Shape {
  registry: SourceRegistry;
  contract: CompositeContract;
}

function buildShape(payloads: { alpha?: unknown; beta?: unknown }): Shape {
  const registry = new SourceRegistry();
  registry.register(
    new FixtureSource('svc-a', { snapshot: true, version: 'v1' })
      .method('read', () => payloads.alpha),
  );
  registry.register(
    new FixtureSource('svc-b', { snapshot: true, version: 'v1' })
      .method('read', () => payloads.beta),
  );
  const contract: CompositeContract = {
    name: 'fieldShape',
    input: [],
    calls: [
      {
        id: 'alpha',
        source: 'svc-a',
        method: 'read',
        dependencies: [],
        buildRequest: () => ({}),
        fields: [
          { output: 'reqValue', requirement: 'required', path: 'reqValue' },
          { output: 'optWithDefault', requirement: 'optional', defaultOnMissing: 'def-7' },
          { output: 'optMissing', requirement: 'optional' },
        ],
      },
      {
        id: 'beta',
        source: 'svc-b',
        method: 'read',
        dependencies: [],
        buildRequest: () => ({}),
        fields: [{ output: 'betaValue', requirement: 'optional' }],
      },
    ],
  };
  return { registry, contract };
}

async function execute(shape: Shape): Promise<CompositeResult> {
  return new ExecutionKernel(shape.registry).execute(shape.contract, {
    input: {},
    timeoutMs: 1000,
    snapshotToken: 'snap-fields',
    runId: 'run-fields',
    log: new RunLog('run-fields', []),
  });
}

describe('field provenance and requirement semantics', () => {
  it('required present + optional default + optional missing all coexist as complete', async () => {
    const shape = buildShape({ alpha: { reqValue: 'x' }, beta: { betaValue: 'b' } });
    const result = await execute(shape);

    assert.equal(result.status, 'complete');
    assert.equal(result.data.reqValue, 'x');
    assert.equal(result.data.optWithDefault, 'def-7');
    const withDefault = result.fields.find((f) => f.field === 'optWithDefault')!;
    assert.equal(withDefault.state, 'present');
    assert.equal(withDefault.defaulted, true);
    const missing = result.fields.find((f) => f.field === 'optMissing')!;
    assert.equal(missing.state, 'missing');
    assert.equal(missing.reason, undefined);
    assert.equal(result.data.optMissing, undefined);
    assert.equal(result.data.betaValue, 'b');
  });

  it('fails when a resolved required value is absent from the payload', async () => {
    const shape = buildShape({ alpha: { reqValue: undefined }, beta: {} });
    const result = await execute(shape);

    assert.equal(result.status, 'failed');
    const field = result.fields.find((f) => f.field === 'reqValue')!;
    assert.equal(field.state, 'failed');
    assert.equal(field.reason?.reason, 'REQUIRED_VALUE_MISSING');
    assert.equal(field.reason?.category, 'COMPUTATION_FAILED');
  });

  it('degrades when an optional source fails, while the sibling branch keeps running', async () => {
    const plan = new FaultPlan().add('svc-b', 'read', {
      type: 'fail',
      reason: 'SVC_B_DOWN',
      message: 'beta forced down',
    });
    const registry = new SourceRegistry();
    registry.register(
      new FixtureSource('svc-a', { snapshot: true, version: 'v1' }).method('read', () => ({
        reqValue: 'x',
      })),
    );
    registry.register(
      new FixtureSource('svc-b', { snapshot: true, version: 'v1' }, { faultPlan: plan }).method(
        'read',
        () => ({ betaValue: 'b' }),
      ),
    );
    const { contract } = buildShape({});
    const result = await new ExecutionKernel(registry).execute(contract, {
      input: {},
      timeoutMs: 1000,
      snapshotToken: 'snap-fields',
      runId: 'run-fields-2',
      log: new RunLog('run-fields-2', []),
    });

    assert.equal(result.status, 'partial');
    assert.equal(result.data.reqValue, 'x');
    const beta = result.fields.find((f) => f.field === 'betaValue')!;
    assert.equal(beta.state, 'failed');
    assert.equal(beta.requirement, 'optional');
    assert.equal(beta.reason?.reason, 'SVC_B_DOWN');
    assert.equal(beta.reason?.category, 'COMPUTATION_FAILED');
    // No run-level error: optional degradation is not a terminal failure.
    assert.equal(result.errors.length, 0);
  });
});
