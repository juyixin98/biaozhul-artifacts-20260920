const {
  sequelize,
  truncateAll,
  makeOrg,
  makeUser,
  seedQuestions,
  bankByDifficulty,
  http,
} = require('./helpers');
const { Certificate } = require('../src/models');

beforeAll(async () => {
  await sequelize.authenticate();
});
beforeEach(truncateAll);
afterAll(async () => sequelize.close());

async function setup(timezone = 'Asia/Shanghai') {
  const org = await makeOrg('A', timezone);
  const org2 = await makeOrg('B', timezone);
  const sup = await makeUser(org.id, 'supervisor');
  const sup2 = await makeUser(org2.id, 'supervisor');
  const student = await makeUser(org.id, 'student');
  const outsider = await makeUser(org2.id, 'student');
  await seedQuestions(bankByDifficulty(8, 20));
  const paper = (await http().post('/api/papers', { userId: sup.id, body: { seed: `cert-${Date.now()}` } })).body;
  return { org, org2, sup, sup2, student, outsider, paper };
}

function allCorrect(paper) {
  const answers = {};
  for (const q of paper.questions) answers[q.position] = q.answer.slice();
  return answers;
}

function nCorrect(paper, n) {
  const answers = {};
  paper.questions.forEach((q, i) => {
    if (i < n) answers[q.position] = q.answer.slice();
    else if (q.type === 'boolean') answers[q.position] = [q.answer[0] === 'true' ? 'false' : 'true'];
    else answers[q.position] = q.answer[0] === 'A' ? ['B'] : ['A'];
  });
  return answers;
}

describe('certificate issuance gates', () => {
  test('score 100 -> issued, valid exactly 12 months; same exam never gets a duplicate', async () => {
    const { student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', {
      userId: student.id, body: { paperId: paper.id }, now: '2026-02-01T00:00:00.000Z',
    })).body;
    const r = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers: allCorrect(paper) }, requestId: 'full', now: '2026-02-01T00:10:00.000Z',
    });
    expect(r.body.score).toBe(100);
    expect(r.body.certificate).toBeTruthy();
    expect(r.body.certificate.status).toBe('valid');
    expect(r.body.certificate.validFrom).toBe('2026-02-01T00:10:00.000Z');
    expect(r.body.certificate.validUntil).toBe('2027-02-01T00:10:00.000Z');

    // Replaying submission does not issue another certificate.
    const replay = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers: allCorrect(paper) }, requestId: 'full', now: '2026-02-01T00:11:00.000Z',
    });
    expect(replay.body.certificate.certNo).toBe(r.body.certificate.certNo);
    expect(await Certificate.count({ where: { examId: start.id } })).toBe(1);
  });

  test('score 75 (below 80) -> no certificate even though mastery gate later could pass', async () => {
    const { student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    const r = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers: nCorrect(paper, 15) }, requestId: '75points',
    });
    expect(r.body.score).toBe(75);
    expect(r.body.certificate).toBeNull();
  });

  test('score 80 but mastery below 85 (first exam 70 then 80) -> no certificate', async () => {
    const { student, paper } = await setup();
    const api = http();

    const e1 = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    await api.post(`/api/exams/${e1.id}/submit`, {
      userId: student.id, body: { answers: nCorrect(paper, 14) }, requestId: 'first-70', // 70
    });
    // Second attempt same day (cap is 3): latest valid score 80, mastery=80 <85.
    const e2 = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    const r2 = await api.post(`/api/exams/${e2.id}/submit`, {
      userId: student.id, body: { answers: nCorrect(paper, 16) }, requestId: 'second-80',
    });
    expect(r2.body.score).toBe(80);
    expect(r2.body.certificate).toBeNull();

    const mastery = await api.get('/api/me/mastery', { userId: student.id });
    expect(mastery.body.mastery).toBe(80);
  });

  test('expired at 12-month boundary reads as expired; one ms before is valid', async () => {
    const { student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', {
      userId: student.id, body: { paperId: paper.id }, now: '2026-02-01T00:00:00.000Z',
    })).body;
    await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers: allCorrect(paper) }, requestId: 'c', now: '2026-02-01T00:00:00.000Z',
    });

    const before = await api.get('/api/me/certificates', { userId: student.id, now: '2027-01-31T23:59:59.999Z' });
    expect(before.body.certificates[0].status).toBe('valid');
    const at = await api.get('/api/me/certificates', { userId: student.id, now: '2027-02-01T00:00:00.000Z' });
    expect(at.body.certificates[0].status).toBe('expired');
  });
});

describe('review and certificate invalidation', () => {
  test('review lowering below 80 revokes certificate; history versions immutable; restore works', async () => {
    const { sup, student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    const graded = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers: allCorrect(paper) }, requestId: 'rev-check',
    });
    expect(graded.body.certificate.status).toBe('valid');

    const review = await api.post(`/api/exams/${start.id}/review`, {
      userId: sup.id, body: { score: 60, reason: '复核：两题标准答案勘误' },
    });
    expect(review.status).toBe(201);
    expect(review.body.version.version).toBe(2);
    expect(review.body.certificate.status).toBe('revoked');
    expect(review.body.certificate.revokeReason).toMatch(/60/);

    const certs = await api.get('/api/me/certificates', { userId: student.id });
    expect(certs.body.certificates[0].status).toBe('revoked');

    // v1 remains 100; the current result reads v2=60.
    const history = await api.get(`/api/exams/${start.id}/history`, { userId: student.id });
    expect(history.body.versions.map((v) => v.score)).toEqual([100, 60]);
    const result = await api.get(`/api/exams/${start.id}/result`, { userId: student.id });
    expect(result.body.score).toBe(60);

    // Mastery follows the latest valid (reviewed) score.
    const mastery = await api.get('/api/me/mastery', { userId: student.id });
    expect(mastery.body.mastery).toBe(60);

    // Restoring to >= 80 via review reinstates validity (same cert row).
    const restore = await api.post(`/api/exams/${start.id}/review`, {
      userId: sup.id, body: { score: 90, reason: '复核恢复：申诉成立' },
    });
    expect(restore.body.certificate.status).toBe('valid');
    expect((await Certificate.findAll({ where: { examId: start.id } }))).toHaveLength(1);
  });

  test('review without reason is rejected; students cannot review', async () => {
    const { student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers: allCorrect(paper) }, requestId: 'p',
    });
    const noReason = await api.post(`/api/exams/${start.id}/review`, {
      userId: student.id, body: { score: 50 },
    });
    expect([400, 403]).toContain(noReason.status);
  });
});

describe('mastery decay (30 days no learning -> -5, floor 0, explicit boundaries)', () => {
  test('day 29 no decay; day 30 exactly -5; day 60 -10; never below 0', async () => {
    const { student, paper } = await setup('UTC');
    const api = http();
    const t0 = '2026-01-01T12:00:00.000Z';
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id }, now: t0 })).body;
    await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers: allCorrect(paper) }, requestId: 'm0', now: t0,
    });

    const d29 = await api.get('/api/me/mastery', { userId: student.id, now: '2026-01-30T11:59:59.999Z' });
    expect(d29.body.mastery).toBe(100);

    const d30 = await api.get('/api/me/mastery', { userId: student.id, now: '2026-01-31T12:00:00.000Z' });
    expect(d30.body.mastery).toBe(95);
    expect(d30.body.inactiveBlocks).toBe(1);

    const d59 = await api.get('/api/me/mastery', { userId: student.id, now: '2026-02-28T11:59:59.999Z' });
    expect(d59.body.mastery).toBe(95);

    const d60 = await api.get('/api/me/mastery', { userId: student.id, now: '2026-03-02T12:00:00.000Z' });
    expect(d60.body.inactiveBlocks).toBe(2);
    expect(d60.body.mastery).toBe(90);

    // 600 days after last activity -> 20 full blocks -> floor at 0.
    const far = await api.get('/api/me/mastery', { userId: student.id, now: '2027-08-24T12:00:00.000Z' });
    expect(far.body.mastery).toBe(0);
  });

  test('a new valid exam resets the decay clock; expired exams are not learning activity', async () => {
    const { student, paper } = await setup('UTC');
    const api = http();
    const t0 = '2026-01-01T12:00:00.000Z';
    const e1 = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id }, now: t0 })).body;
    await api.post(`/api/exams/${e1.id}/submit`, {
      userId: student.id, body: { answers: allCorrect(paper) }, requestId: 'a1', now: t0,
    });

    // An expired attempt at day 20 (never submitted) must NOT refresh activity.
    const e2 = (await api.post('/api/exams', {
      userId: student.id, body: { paperId: paper.id }, now: '2026-01-21T12:00:00.000Z',
    })).body;
    await api.post(`/api/exams/${e2.id}/timeout`, {
      userId: student.id, now: '2026-01-21T12:30:00.000Z',
    });

    const d31 = await api.get('/api/me/mastery', { userId: student.id, now: '2026-02-01T12:00:00.000Z' });
    expect(d31.body.mastery).toBe(95); // still measured from Jan 1
  });
});

describe('authorization', () => {
  test('students only see their own exams; supervisors are scoped to their organization', async () => {
    const { sup, sup2, student, outsider, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;

    expect((await api.get(`/api/exams/${start.id}`, { userId: outsider.id })).status).toBe(403);
    expect((await api.get(`/api/exams/${start.id}/result`, { userId: outsider.id })).status).toBe(403);

    // Own supervisor can read.
    expect((await api.get(`/api/exams/${start.id}/result`, { userId: sup.id })).status).toBe(200);
    // Other-org supervisor cannot.
    expect((await api.get(`/api/exams/${start.id}/result`, { userId: sup2.id })).status).toBe(403);

    const list = await api.get('/api/exams', { userId: sup.id });
    expect(list.body.exams.every((e) => e.userId === student.id)).toBe(true);
    const list2 = await api.get('/api/exams', { userId: sup2.id });
    expect(list2.body.exams).toHaveLength(0);
  });

  test('supervisor cannot review exams of another organization', async () => {
    const { sup2, student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers: allCorrect(paper) }, requestId: 'z',
    });
    const r = await api.post(`/api/exams/${start.id}/review`, {
      userId: sup2.id, body: { score: 50, reason: '越权复核' },
    });
    expect(r.status).toBe(403);
  });
});
