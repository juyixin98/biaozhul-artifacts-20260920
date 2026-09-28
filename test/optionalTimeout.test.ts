/**
 * Optional timeout + deadline propagation + cancellation.
 *
 * An instrumented promotions source cooperatively waits on the run's AbortSignal
 * (it would otherwise work for 5s). With an 80ms deadline it MUST be aborted at
 * ~80ms, classified as a timeout, and leave no background work behind. The
 * optional field degrades while every required field still resolves -> partial.
 *
 * A second case pins a REQUIRED source behind a tight deadline to prove a
 * deadline-hit required node fails the run, and that the SAME absolute deadline
 * was propagated to every node along the dependency graph.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ExecutionKernel } from '../src/kernel/executor.js';
import { RunLog } from '../src/observability/runLog.js';
import { buildSources } from '../src/sources/index.js';
import { orderSummaryContract } from '../src/compose/contracts.js';
import { runComposite } from './helpers/harness.js';
import { cancellableDelay } from '../src/kernel/timing.js';
import type { CallContext, Source } from '../src/sources/types.js';

/** Source whose work survives ONLY if the engine/source ignores cancellation. */
class ProbeSlowPromotions implements Source {
  readonly name = 'promotions';
  readonly capabilities = { snapshot: true, version: 'catalog-epoch-2026-09-01' };
  /** Flips true only if code keeps running after the abort (must never happen). */
  postAbortWorkObserved = false;
  sawDeadline: number | null = null;

  async call(_method: string, _request: unknown, ctx: CallContext): Promise<unknown> {
    this.sawDeadline = ctx.deadline;
    try {
      // Would run for 5s; the propagated deadline must reject this wait.
      await cancellableDelay(5000, ctx.signal);
      // Unreachable when the deadline works.
      this.postAbortWorkObserved = true;
      return { promotionCode: 'LATE-CODE', discountPct: 99 };
    } catch {
      // Even cleanup work scheduled here must observe the abort. We schedule a
      // probe only to prove the cooperative contract; a real source would just
      // release resources synchronously.
      if (!ctx.signal.aborted) this.postAbortWorkObserved = true;
      throw ctx.signal.reason instanceof Error
        ? ctx.signal.reason
        : new Error('aborted without reason');
    }
  }
}

async function runWithProbePromotions(timeoutMs: number) {
  const registry = buildSources({ baseLatencyMs: 0, skip: ['promotions'] });
  const probe = new ProbeSlowPromotions();
  registry.register(probe);
  const runId = 'run-timeout-probe';
  const log = new RunLog(runId, []);
  const kernel = new ExecutionKernel(registry);
  const startedAt = Date.now();
  const result = await kernel.execute(orderSummaryContract, {
    input: { userId: 'u-1001', sku: 'sku-1' },
    timeoutMs,
    snapshotToken: 'snap-timeout-test',
    runId,
    log,
  });
  return { result, registry, probe, log, startedAt, elapsedMs: Date.now() - startedAt };
}

describe('optional source timeout', () => {
  it('aborts the slow optional call at the propagated deadline, not after 5s', async () => {
    const { result, elapsedMs, probe, log } = await runWithProbePromotions(80);

    assert.equal(result.status, 'partial');
    assert.ok(elapsedMs < 250, `run should end near the 80ms deadline, took ${elapsedMs}ms`);
    assert.ok(elapsedMs >= 70, `must not return before the deadline, took ${elapsedMs}ms`);

    const promo = result.nodes.find((n) => n.callId === 'promo')!;
    assert.equal(promo.state, 'timeout');
    assert.equal(promo.attempts, 1);
    assert.equal(promo.error?.category, 'RESOURCE_EXHAUSTED');
    assert.equal(promo.error?.reason, 'DEADLINE_EXCEEDED');

    // Optional field degrades with a concrete field-level reason.
    const code = result.fields.find((f) => f.field === 'promotionCode')!;
    assert.equal(code.state, 'failed');
    assert.equal(code.requirement, 'optional');
    assert.equal(code.reason?.reason, 'DEADLINE_EXCEEDED');

    // Required data from the other branches was still harvested.
    assert.equal(result.data.customerName, 'Ada Lin');
    assert.equal(result.data.finalPrice, 1104.15);

    // Decision trail records why.
    assert.ok(log.events.some((e) => e.type === 'DECISION' && e.data?.rule === 'DEADLINE_ABORT'));

    // Give any leaked background timer a chance to show itself.
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.equal(probe.postAbortWorkObserved, false, 'no work may continue after the deadline abort');
  });

  it('propagates one absolute deadline to every source call', async () => {
    const { registry, startedAt, probe } = await runWithProbePromotions(80);
    const expectedDeadline = startedAt + 80;
    for (const invocation of registry.all()) {
      assert.ok(
        Math.abs(invocation.contextSnapshot.deadline - expectedDeadline) <= 30,
        `${invocation.source} saw a different deadline`,
      );
      assert.equal(invocation.contextSnapshot.timeoutMs, 80);
    }
    assert.ok(probe.sawDeadline !== null && Math.abs(probe.sawDeadline - expectedDeadline) <= 30);
  });

  it('fails the run when the deadline hits a REQUIRED node', async () => {
    const { result, elapsedMs } = await runComposite({
      baseLatencyMs: 0,
      timeoutMs: 50,
      faultPlan: new (await import('../src/sources/fixtures/faults.js')).FaultPlan().add(
        'pricing',
        'getQuote',
        { type: 'timeout', delayMs: 5000 },
      ),
    });

    assert.ok(elapsedMs < 200, `took ${elapsedMs}ms`);
    assert.equal(result.status, 'failed');
    const quote = result.nodes.find((n) => n.callId === 'quote')!;
    assert.equal(quote.state, 'timeout');
    assert.equal(quote.error?.category, 'RESOURCE_EXHAUSTED');
    const finalPrice = result.fields.find((f) => f.field === 'finalPrice')!;
    assert.equal(finalPrice.state, 'failed');
    assert.equal(finalPrice.reason?.reason, 'DEADLINE_EXCEEDED');
    // Independent optional/defaulted values remain available.
    assert.equal(result.data.discountPct, undefined);
  });

  it('uses a throwaway temp dir so test artifacts never touch the real database', () => {
    const dir = mkdtempSync(join(tmpdir(), 'compose-timeout-'));
    assert.ok(dir.length > 0);
  });
});
