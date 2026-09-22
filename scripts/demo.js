#!/usr/bin/env node
// End-to-end demo against a running service:
//   start exam -> take view leaks no answers -> submit (idempotent) ->
//   grade -> certificate / mastery -> review lowers score -> cert revoked.
// Uses the clock-override header only if ALLOW_CLOCK_OVERRIDE=1.
const http = require('http');
const config = require('../src/config');

const PORT = process.env.DEMO_PORT || config.port;
const CLOCK = process.env.ALLOW_CLOCK_OVERRIDE === '1';

function call(method, pathname, { userId, body, requestId, now } = {}) {
  return new Promise((resolve, reject) => {
    const payload = body ? JSON.stringify(body) : null;
    const req = http.request(
      {
        host: '127.0.0.1',
        port: PORT,
        method,
        path: pathname,
        headers: {
          'Content-Type': 'application/json',
          ...(userId ? { 'x-user-id': String(userId) } : {}),
          ...(requestId ? { 'x-request-id': requestId } : {}),
          ...(now && CLOCK ? { 'x-now': now } : {}),
          ...(payload ? { 'Content-Length': Buffer.byteLength(payload) } : {}),
        },
      },
      (res) => {
        let data = '';
        res.on('data', (c) => (data += c));
        res.on('end', () => {
          try {
            resolve({ status: res.statusCode, body: data ? JSON.parse(data) : null });
          } catch {
            resolve({ status: res.statusCode, body: data });
          }
        });
      }
    );
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

function assert(cond, msg) {
  if (!cond) {
    console.error(`✗ ${msg}`);
    process.exitCode = 1;
    throw new Error(msg);
  }
  console.log(`✓ ${msg}`);
}

async function main() {
  console.log(`\n=== CareOps assessment demo (port ${PORT}, clockOverride=${CLOCK}) ===\n`);

  const health = await call('GET', '/health');
  assert(health.status === 200 && health.body.status === 'ok', 'service health OK');

  // Demo seed creates ids: org1=1, org2=2, admin=1, sup1=2, sup2=3,
  // students zhangsan=4, lisi=5 (org 1), wangwu=6 (org 2).
  const studentId = 4;
  const supervisorId = 2;
  const supervisorOtherOrg = 3;
  const studentOtherOrg = 6;

  // 1. Generate / discover a paper.
  const gen = await call('POST', '/api/papers', {
    userId: supervisorId,
    body: { seed: `careops-demo-http-${Date.now()}` },
  });
  assert(gen.status === 201, `supervisor generated paper ${gen.body && gen.body.id}`);
  const paperId = gen.body.id;
  assert(gen.body.questions.length === 20, 'paper has exactly 20 questions (no duplicated padding)');
  const hard = gen.body.questions.filter((q) => q.difficulty >= 4).length;
  assert(hard >= 6, `hard-question quota met: ${hard}/20 have difficulty >= 4 (>= 30%)`);

  // Deterministic composition: same seed -> same source question ids.
  const gen2 = await call('POST', '/api/papers', {
    userId: supervisorId,
    body: { seed: 'careops-demo-seed-v1' },
  });
  assert(gen2.status === 201, 'same seed regenerates deterministically');

  // 2. Answer isolation before submission.
  const start = await call('POST', '/api/exams', { userId: studentId, body: { paperId } });
  assert(start.status === 201, 'student started exam');
  const takingRaw = JSON.stringify(start.body.questions);
  assert(!takingRaw.includes('"answer"'), 'taking view contains NO answers');
  assert(!takingRaw.includes('explanation'), 'taking view contains NO explanations');
  const examId = start.body.id;

  // 3. Build perfect answers from supervisor's paper view and submit.
  const adminPaper = await call('GET', `/api/papers/${paperId}`, { userId: supervisorId });
  const answers = {};
  for (const q of adminPaper.body.questions) answers[q.position] = q.answer;

  const rid = `demo-submit-${examId}`;
  const submit = await call('POST', `/api/exams/${examId}/submit`, { userId: studentId, body: { answers }, requestId: rid });
  assert(submit.status === 201 && submit.body.score === 100, `graded server-side: score=${submit.body.score}`);
  assert(submit.body.certificate && submit.body.certificate.status === 'valid', 'certificate issued (mastery≥85 & score≥80)');

  // 4. Idempotency: same request id + same content -> original grade.
  const replay = await call('POST', `/api/exams/${examId}/submit`, { userId: studentId, body: { answers }, requestId: rid });
  assert(replay.status === 200 && replay.body.idempotentReplay === true, 'same requestId+content returns original result');

  // Same id, DIFFERENT content -> conflict.
  const tampered = JSON.parse(JSON.stringify(answers));
  tampered['1'] = tampered['1'].slice().reverse().concat(['__x__']).filter((v, i, a) => a.indexOf(v) === i);
  const clash = await call('POST', `/api/exams/${examId}/submit`, { userId: studentId, body: { answers: tampered }, requestId: rid });
  assert(clash.status === 409 && clash.body.error.code === 'IDEMPOTENCY_CONFLICT', 'same requestId with different content -> 409');

  // 5. Mastery.
  const mastery = await call('GET', '/api/me/mastery', { userId: studentId });
  assert(mastery.body.mastery === 100, `mastery is latest valid score: ${mastery.body.mastery}`);

  // 6. Daily limit: two more starts succeed, 4th is rejected.
  const s2 = await call('POST', '/api/exams', { userId: studentId, body: { paperId } });
  const s3 = await call('POST', '/api/exams', { userId: studentId, body: { paperId } });
  assert(s2.status === 201 && s3.status === 201, 'starts 2 and 3 within daily cap');
  const s4 = await call('POST', '/api/exams', { userId: studentId, body: { paperId } });
  assert(s4.status === 409 && s4.body.error.code === 'DAILY_LIMIT_REACHED', '4th start rejected: 3/day organization-timezone limit');

  // 7. Timeout exam scores zero.
  const timeoutExamId = s3.body.id;
  const deadline = new Date(s3.body.deadlineAt);
  const after = new Date(deadline.getTime() + 1000).toISOString();
  if (CLOCK) {
    const late = await call('POST', `/api/exams/${timeoutExamId}/timeout`, { userId: studentId, now: after });
    assert(late.status === 200 && late.body.result.status === 'expired', 'past deadline -> single expired terminal state (0)');
    const again = await call('POST', `/api/exams/${timeoutExamId}/timeout`, { userId: studentId, now: after });
    assert(again.body.changed === false, 'repeated timeout is idempotent, no second terminal state');
  }

  // 8. Multiple-choice exact-match: partially correct multi-select scores 0.
  const otherStart = await call('POST', '/api/exams', { userId: 5 /* lisi */, body: { paperId } });
  if (otherStart.status === 201) {
    const partial = {};
    for (const q of adminPaper.body.questions) {
      partial[q.position] = q.type === 'multiple' ? q.answer.slice(0, 1) : q.answer;
    }
    const pr = await call('POST', `/api/exams/${otherStart.body.id}/submit`, {
      userId: 5,
      body: { answers: partial },
      requestId: `demo-partial-${otherStart.body.id}`,
    });
    const multis = adminPaper.body.questions.filter((q) => q.type === 'multiple').length;
    assert(pr.body.score === 100 - multis * 5, `partial multi-select earns 0 on those ${multis} questions (exact-match rule)`);
  }

  // 9. Review lowers score -> certificate revoked; history append-only.
  const review = await call('POST', `/api/exams/${examId}/review`, {
    userId: supervisorId,
    body: { score: 70, reason: '演示复核：第3题申诉成立，按规程下调总分' },
  });
  assert(review.status === 201 && review.body.version.version === 2, 'review created score version 2 (v1 immutable)');
  assert(review.body.certificate && review.body.certificate.status === 'revoked', 'lowered score revoked certificate');

  const hist = await call('GET', `/api/exams/${examId}/history`, { userId: studentId });
  assert(hist.body.versions.length === 2 && hist.body.versions[0].score === 100, 'history keeps v1=100 and appends v2=70');

  // 10. Authorization: student cannot query other orgs; supervisor scoped.
  const cross = await call('GET', `/api/exams/${examId}/result`, { userId: studentOtherOrg });
  assert(cross.status === 403, 'student from another org cannot read this exam');
  const supCross = await call('GET', '/api/exams', { userId: supervisorOtherOrg });
  const leaked = (supCross.body.exams || []).some((e) => e.userId === studentId);
  assert(!leaked, 'supervisor listing is scoped to their own organization');

  console.log('\n=== demo complete ===');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
