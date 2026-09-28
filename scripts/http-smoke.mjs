/**
 * End-to-end smoke verification against a RUNNING server (started by
 * scripts/verify.sh). Uses only the global fetch — no test framework.
 *
 * Every assertion checks a concrete result or a concrete failure category;
 * nothing here is satisfied by "the endpoint responded".
 *
 * Usage: node scripts/http-smoke.mjs [baseUrl]
 */

const base = process.argv[2] ?? 'http://127.0.0.1:3000';
let failures = 0;

function check(name, condition, detail) {
  if (condition) {
    process.stdout.write(`  ok   ${name}\n`);
  } else {
    failures += 1;
    process.stdout.write(`  FAIL ${name}\n        ${detail ?? ''}\n`);
  }
}

async function call(method, url, body, requestId) {
  const headers = { 'content-type': 'application/json' };
  if (requestId) headers['x-request-id'] = requestId;
  const res = await fetch(base + url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = res.status === 204 ? undefined : await res.json().catch(() => undefined);
  return { status: res.status, json };
}

function eq(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

async function main() {
  process.stdout.write(`\nHTTP smoke against ${base}\n`);

  // 1. Create with empty + escaped keys.
  const created = await call('POST', '/documents', {
    id: 'shop',
    document: { list: [1, 2, 3], 'a/b': 1, '': 'root-empty', n: 0 },
  }, 'smoke-create');
  check('create -> 201 v0', created.status === 201 && created.json.version === 0,
    `${created.status} ${JSON.stringify(created.json)}`);

  // 2. Array shift at head + append via '-', concrete ordering.
  const shifted = await call('POST', '/documents/shop/patch', {
    expectedVersion: 0,
    patch: [
      { op: 'add', path: '/list/0', value: 9 },
      { op: 'add', path: '/list/-', value: 4 },
      { op: 'test', path: '/a~1b', value: 1 },
      { op: 'replace', path: '/', value: 'seen' },
    ],
  }, 'smoke-shift');
  check('array head-shift + dash-append + escaped keys',
    shifted.status === 200 &&
      eq(shifted.json.result.list, [9, 1, 2, 3, 4]) &&
      shifted.json.result['a/b'] === 1 &&
      shifted.json.result[''] === 'seen' &&
      shifted.json.newVersion === 1 &&
      shifted.json.steps.length === 4,
    `${shifted.status} ${JSON.stringify(shifted.json?.error ?? shifted.json?.result)}`);

  // 3. Mid-patch test failure -> 422, typed, rolled back, version unchanged.
  const failed = await call('POST', '/documents/shop/patch', {
    expectedVersion: 1,
    patch: [
      { op: 'add', path: '/list/-', value: 100 },
      { op: 'replace', path: '/n', value: 7 },
      { op: 'test', path: '/n', value: 777 },
      { op: 'remove', path: '/list/0' },
    ],
  }, 'smoke-fail');
  check('mid-patch test failure -> TEST_FAILED @index2, rolled back',
    failed.status === 422 &&
      failed.json.error.category === 'TEST_FAILED' &&
      failed.json.error.failedAtIndex === 2 &&
      failed.json.error.rolledBack === true &&
      failed.json.error.appliedBeforeFailure.length === 2,
    JSON.stringify(failed.json?.error));

  const afterFail = await call('GET', '/documents/shop');
  check('document unchanged after failure (v1)',
    afterFail.json.version === 1 &&
      eq(afterFail.json.document.list, [9, 1, 2, 3, 4]) &&
      afterFail.json.document.n === 0,
    JSON.stringify(afterFail.json));

  // 4. Stale version binding -> 409 VERSION_CONFLICT with concrete versions.
  const conflict = await call('POST', '/documents/shop/patch', {
    expectedVersion: 0,
    patch: [{ op: 'replace', path: '/n', value: 1 }],
  }, 'smoke-conflict');
  check('stale binding -> 409 VERSION_CONFLICT (0 vs 1)',
    conflict.status === 409 &&
      conflict.json.error.category === 'VERSION_CONFLICT' &&
      conflict.json.error.expectedVersion === 0 &&
      conflict.json.error.actualVersion === 1,
    JSON.stringify(conflict.json?.error));

  // 5. move into descendant -> 400 at contract layer.
  const moveBad = await call('POST', '/documents/shop/patch', {
    expectedVersion: 1,
    patch: [{ op: 'move', from: '/list', path: '/list/2' }],
  }, 'smoke-move-desc');
  check('move into descendant -> 400 MOVE_INTO_SELF',
    moveBad.status === 400 && moveBad.json.error.category === 'MOVE_INTO_SELF',
    JSON.stringify(moveBad.json?.error));

  // 6. Exact self-move is rejected by this constrained service (documented).
  const selfMove = await call('POST', '/documents/shop/patch', {
    expectedVersion: 1,
    patch: [{ op: 'move', from: '/n', path: '/n' }],
  }, 'smoke-move-self');
  check('exact self-move -> 400 MOVE_INTO_SELF (constrained choice)',
    selfMove.status === 400 && selfMove.json.error.category === 'MOVE_INTO_SELF',
    JSON.stringify(selfMove.json?.error));

  // 7. move reindex semantics: /0 -> /1 uses post-removal position.
  const moveOk = await call('POST', '/documents/shop/patch', {
    expectedVersion: 1,
    patch: [{ op: 'move', from: '/list/0', path: '/list/1' }],
  }, 'smoke-move-ok');
  check('move /0 -> /1 yields post-removal ordering',
    moveOk.status === 200 && eq(moveOk.json.result.list, [1, 9, 2, 3, 4]),
    JSON.stringify(moveOk.json?.result ?? moveOk.json?.error));

  // 8. Operations take values from the previous step's result.
  const chained = await call('POST', '/documents/shop/patch', {
    expectedVersion: 2,
    patch: [
      { op: 'add', path: '/scratch', value: { nested: [1, 2] } },
      { op: 'copy', from: '/scratch/nested', path: '/copied' },
      { op: 'test', path: '/copied/1', value: 2 },
      { op: 'move', from: '/scratch/nested/0', path: '/copied/-' },
    ],
  }, 'smoke-chain');
  check('chained ops read prior results (copy/move/test)',
    chained.status === 200 &&
      eq(chained.json.result.copied, [1, 2, 1]) &&
      eq(chained.json.result.scratch.nested, [2]),
    `${chained.status} ${JSON.stringify(chained.json?.error ?? chained.json?.result)}`);

  // 9. Diagnostics: event lookup by request id, failure recorded independently.
  const event = await call('GET', '/events/smoke-fail');
  check('diagnostic event for failed request id',
    event.status === 200 &&
      event.json.event.status === 'rejected' &&
      event.json.event.category === 'TEST_FAILED' &&
      event.json.event.stepsAppliedBeforeOutcome === 2 &&
      event.json.event.newVersion === null,
    JSON.stringify(event.json));

  const events = await call('GET', '/documents/shop/events');
  const ids = events.json.events.map((e) => e.requestId);
  check('event history contains applied + rejected + conflict request ids',
    events.status === 200 &&
      ids.includes('smoke-shift') &&
      ids.includes('smoke-fail') &&
      ids.includes('smoke-move-ok'),
    JSON.stringify(ids));

  process.stdout.write(`\n${failures === 0 ? 'ALL SMOKE CHECKS PASSED' : failures + ' SMOKE CHECK(S) FAILED'}\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  process.stderr.write(`smoke harness error: ${error?.stack ?? error}\n`);
  process.exit(2);
});
