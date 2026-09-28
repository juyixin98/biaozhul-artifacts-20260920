/* eslint-disable no-console */
/**
 * Local end-to-end demo. Uses only synthetic local fixtures and an in-memory
 * SQLite database. Runs the four required scenarios and also exercises the
 * running HTTP service + diagnostic endpoints.
 *
 *   npm run demo
 */
import { buildScenario, HAPPY_INPUT, snapshotToken } from '../src/fixtures/scenario.js';
import { CompositeKernel } from '../src/kernel/executor.js';
import { BoundedSemaphore } from '../src/kernel/semaphore.js';
import { RunStore } from '../src/state/runStore.js';
import { buildApp } from '../src/service/app.js';
import { ContractRegistry } from '../src/service/registry.js';
import type { Scenario } from '../src/fixtures/scenario.js';
import type { CompositeResponse } from '../src/types.js';

async function execute(
  label: string,
  scenario: Scenario,
  options: {
    timeoutMs?: number;
    snapshotToken?: string | null;
    params?: Record<string, unknown>;
  } = {},
): Promise<CompositeResponse> {
  const store = new RunStore(':memory:');
  const kernel = new CompositeKernel({
    sources: scenario.sources,
    semaphore: new BoundedSemaphore(4, 16),
    store,
  });
  const response = await kernel.run(scenario.contract, {
    runId: `demo-${label}`,
    timeoutMs: options.timeoutMs ?? 1000,
    snapshotToken: options.snapshotToken === undefined ? snapshotToken('v1') : options.snapshotToken,
    params: options.params ?? HAPPY_INPUT,
  });

  console.log('\n' + '='.repeat(78));
  console.log(`SCENARIO: ${label}   runId=${response.runId}   outcome=${response.outcome}`);
  console.log('-'.repeat(78));
  console.log('data:        ', JSON.stringify(response.data));
  console.log(
    'nodes:       ',
    response.nodeResults
      .map((n) => `${n.nodeId}=${n.status}(attempts=${n.attempts},invoked=${n.didNotInvoke ? 0 : 1})`)
      .join('  '),
  );
  console.log(
    'fields:      ',
    response.fields.map((f) => `${f.path}[${f.required ? 'R' : 'O'}]:${f.status}`).join('  '),
  );
  console.log(
    'consistency: ',
    response.consistency.length === 0
      ? '(none)'
      : response.consistency.map((c) => `${c.kind}@${c.nodeId}`).join('  '),
  );
  console.log(
    'dataVersion: ',
    `requested=${response.dataVersion.requested} observed=[${response.dataVersion.observed.join(',')}] consistent=${response.dataVersion.consistent}`,
  );
  console.log(
    'failures:    ',
    response.failures.length === 0
      ? '(none)'
      : response.failures.map((f) => `${f.category}/${f.code}@${f.at}`).join('  '),
  );
  return response;
}

async function httpDemo(): Promise<void> {
  console.log('\n' + '#'.repeat(78));
  console.log('HTTP SERVICE DEMO');
  console.log('#'.repeat(78));

  const scenario = buildScenario();
  const registry = new ContractRegistry();
  registry.register(scenario.rawContract);
  const store = new RunStore(':memory:');
  const app = await buildApp({ registry, sources: scenario.sources, store, logger: false });
  await app.listen({ port: 0, host: '127.0.0.1' });
  const address = app.server.address();
  const port = typeof address === 'object' && address ? address.port : 0;
  const base = `http://127.0.0.1:${port}`;

  const post = async (path: string, body: unknown): Promise<{ status: number; json: unknown }> => {
    const res = await fetch(base + path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    return { status: res.status, json: await res.json() };
  };
  const get = async (path: string): Promise<{ status: number; json: unknown }> => {
    const res = await fetch(base + path);
    return { status: res.status, json: await res.json() };
  };

  const happy = await post('/query/orderDetails', {
    params: HAPPY_INPUT,
    snapshotToken: snapshotToken('v1'),
    timeoutMs: 1000,
    runId: 'demo-http-happy',
  });
  console.log(`POST /query/orderDetails -> ${happy.status}`);
  const happyJson = happy.json as CompositeResponse;
  console.log('  data:', JSON.stringify(happyJson.data));

  const bad = await post('/query/orderDetails', { params: { customerId: 'C1' }, timeoutMs: 1000 });
  console.log(`POST /query/orderDetails (missing sku) -> ${bad.status}`, JSON.stringify(bad.json));

  const events = await get(`/runs/demo-http-happy/events`);
  const eventsJson = events.json as { events: Array<{ type: string; nodeId?: string }> };
  console.log(`GET  /runs/demo-http-happy/events -> ${events.status} (${eventsJson.events.length} events)`);
  console.log(
    '  sequence:',
    eventsJson.events.map((e) => (e.nodeId ? `${e.type}:${e.nodeId}` : e.type)).join(' -> '),
  );

  const runs = await get('/runs');
  console.log('GET  /runs ->', runs.status, JSON.stringify((runs.json as { count: number }).count) + ' run(s)');

  await app.close();
  store.close();
  scenario.db.close();
}

async function main(): Promise<void> {
  // 1) Diamond happy path: recommendations has parents customers + inventory
  //    + optional promotions, yet must be invoked exactly once.
  await execute('1-diamond-happy', buildScenario());

  // 2) Required failure: pricing errors for SKU-BROKEN -> price/total/label
  //    aggregation is partial with required-field reasons.
  await execute('2-required-failure', buildScenario({ pricingFailForSku: 'SKU-BROKEN' }), {
    snapshotToken: snapshotToken('v1'),
    params: { customerId: 'C1', sku: 'SKU-BROKEN' },
  });

  // 3) Optional timeout: promotions hangs past its 80ms node deadline; the
  //    optional discount defaults to 0 and downstream recommendations still runs.
  await execute('3-optional-timeout', buildScenario({ promotionsHang: true, promotionsLatencyMs: 5000 }));

  // 4) Source version inconsistency: pricing serves v2 under a v1 token and
  //    recommendations cannot snapshot at all -> consistency-limited notes.
  await execute(
    '4-version-inconsistency',
    buildScenario({ pricingVersion: 'v2', recommendationsSupportsSnapshot: false }),
  );

  // 5) Run-wide deadline: 10ms is shorter than the diamond's critical path
  //    (customers 8ms -> inventory 12ms -> recommendations). Nodes that miss
  //    the propagated deadline are cancelled; nothing keeps running after.
  await execute('5-run-deadline', buildScenario(), { timeoutMs: 10 });

  await httpDemo();

  console.log('\nDemo finished.\n');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
