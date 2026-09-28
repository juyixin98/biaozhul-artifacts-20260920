#!/usr/bin/env node
/**
 * End-to-end scenario driver against a RUNNING server.
 *
 * It asserts CONCRETE results (exact documents, versions, categories and
 * statuses) and request-id correlation, not merely that endpoints respond.
 * Exits non-zero on the first failed expectation.
 *
 * Usage: BASE_URL=http://127.0.0.1:8091 node scripts/e2e-scenario.mjs
 */

const BASE_URL = process.env.BASE_URL ?? 'http://127.0.0.1:8091';

let failures = 0;

function check(label, condition, detail) {
  if (condition) {
    process.stdout.write(`  ok   ${label}\n`);
  } else {
    failures += 1;
    process.stdout.write(`  FAIL ${label}${detail ? ` :: ${detail}` : ''}\n`);
  }
}

async function req(method, url, body, headers = {}) {
  const res = await fetch(`${BASE_URL}${url}`, {
    method,
    headers: body === undefined ? headers : { 'content-type': 'application/json', ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = await res.json().catch(() => ({}));
  return { status: res.status, json };
}

async function waitForHealthy(retries = 50) {
  for (let i = 0; i < retries; i += 1) {
    try {
      const r = await fetch(`${BASE_URL}/health`);
      if (r.ok) return;
    } catch {
      // server not up yet
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`server at ${BASE_URL} never became healthy`);
}

async function main() {
  await waitForHealthy();
  process.stdout.write(`\n[e2e] driving ${BASE_URL}\n`);

  // 1. Create a document.
  const seed = {
    title: 'library',
    tags: ['alpha', 'beta', 'gamma'],
    shelf: { books: [{ isbn: '1' }] },
    v: 0,
  };
  let r = await req('POST', '/documents', { id: 'e2e-lib', doc: seed });
  check('create document -> 200', r.status === 200, r.status);
  check('created at version 0', r.json?.data?.version === 0);

  // 2. Successful array-shift patch bound to version 0.
  const ridSuccess = 'e2e-success-1';
  r = await req(
    'POST',
    '/documents/e2e-lib/patch',
    {
      expectedVersion: 0,
      requestId: ridSuccess,
      patch: [
        { op: 'add', path: '/tags/1', value: 'inserted' },
        { op: 'test', path: '/tags/2', value: 'beta' },
        { op: 'add', path: '/tags/-', value: 'tail' },
      ],
    },
    { 'x-request-id': ridSuccess },
  );
  check('shift patch -> 200', r.status === 200, r.status);
  check('fromVersion 0 -> toVersion 1', r.json?.meta?.fromVersion === 0 && r.json?.meta?.toVersion === 1);
  check(
    'shift result exact',
    JSON.stringify(r.json?.data?.result?.tags) === JSON.stringify(['alpha', 'inserted', 'beta', 'gamma', 'tail']),
    JSON.stringify(r.json?.data?.result?.tags),
  );

  // 3. Mid-patch test failure must roll back and not advance the version.
  const ridFail = 'e2e-fail-1';
  r = await req('POST', '/documents/e2e-lib/patch', {
    expectedVersion: 1,
    requestId: ridFail,
    patch: [
      { op: 'replace', path: '/title', value: 'MUTATED' },
      { op: 'add', path: '/tags/-', value: 'should-rollback' },
      { op: 'test', path: '/v', value: 999 },
      { op: 'add', path: '/never', value: true },
    ],
  });
  check('mid-failure -> 409', r.status === 409, r.status);
  check('category TEST_FAILURE', r.json?.error?.category === 'TEST_FAILURE');
  check('failedAtIndex = 2', r.json?.meta?.failedAtIndex === 2);
  const statuses = (r.json?.meta?.steps ?? []).map((s) => s.status);
  check('step statuses applied/applied/failed/skipped', JSON.stringify(statuses) === JSON.stringify(['applied', 'applied', 'failed', 'skipped']), JSON.stringify(statuses));

  // Document must be unchanged and still at version 1.
  r = await req('GET', '/documents/e2e-lib');
  check('rollback keeps version 1', r.json?.data?.version === 1, r.json?.data?.version);
  check('rollback keeps exact pre-failure document', r.json?.data?.doc?.title === 'library');
  check(
    'rollback dropped the appended tag',
    JSON.stringify(r.json?.data?.doc?.tags) === JSON.stringify(['alpha', 'inserted', 'beta', 'gamma', 'tail']),
    JSON.stringify(r.json?.data?.doc?.tags),
  );

  // 4. Stale expectedVersion is rejected before execution.
  r = await req('POST', '/documents/e2e-lib/patch', {
    expectedVersion: 0,
    patch: [{ op: 'replace', path: '/title', value: 'stale' }],
  });
  check('stale version -> 409 VERSION_CONFLICT', r.status === 409 && r.json?.error?.category === 'VERSION_CONFLICT');

  // 5. move into a descendant is rejected.
  r = await req('POST', '/documents/e2e-lib/patch', {
    expectedVersion: 1,
    patch: [{ op: 'move', from: '/shelf', path: '/shelf/books/-' }],
  });
  check('move descendant -> 409 MOVE_INTO_DESCENDANT', r.status === 409 && r.json?.error?.category === 'MOVE_INTO_DESCENDANT');

  // 6. Diagnostics correlation by request id.
  r = await req('GET', `/diagnostics/requests/${ridFail}`);
  check('diagnostics found failed request', r.status === 200 && r.json?.data?.ok === false);
  check('audit carries traces', Array.isArray(r.json?.data?.traces) && r.json.data.traces.length === 4);
  check('audit toVersion stays null on failure', r.json?.data?.toVersion === null);

  r = await req('GET', `/diagnostics/requests/${ridSuccess}`);
  check('diagnostics found success request', r.status === 200 && r.json?.data?.ok === true);
  check('audit records version 0->1', r.json?.data?.fromVersion === 0 && r.json?.data?.toVersion === 1);

  // 7. History lists both attempts newest-first.
  r = await req('GET', '/documents/e2e-lib/history?limit=10');
  const ids = (r.json?.data?.records ?? []).map((x) => x.requestId);
  check('history includes both request ids', ids.includes(ridSuccess) && ids.includes(ridFail), JSON.stringify(ids));

  process.stdout.write(`\n[e2e] ${failures === 0 ? 'ALL SCENARIO CHECKS PASSED' : `${failures} CHECK(S) FAILED`}\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  process.stderr.write(`[e2e] fatal: ${err?.stack ?? err}\n`);
  process.exit(1);
});
