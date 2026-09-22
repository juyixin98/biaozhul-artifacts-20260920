const {
  sequelize,
  truncateAll,
  makeOrg,
  makeUser,
  seedQuestions,
  bankByDifficulty,
  http,
  buildAnswers,
} = require('./helpers');
const { Exam, Organization } = require('../src/models');

beforeAll(async () => {
  await sequelize.authenticate();
});
beforeEach(truncateAll);
afterAll(async () => sequelize.close());

async function setupPaper(role = 'supervisor') {
  const org = await makeOrg('A', 'Asia/Shanghai');
  const sup = await makeUser(org.id, role);
  const student = await makeUser(org.id, 'student');
  await seedQuestions(bankByDifficulty(8, 20));
  const paper = (await http().post('/api/papers', { userId: sup.id, body: { seed: `p-${Date.now()}-${Math.random()}` } })).body;
  return { org, sup, student, paper };
}

describe('daily attempt limit (3/day, organization timezone)', () => {
  test('four sequential starts -> first three succeed, fourth rejected', async () => {
    const { student, paper } = await setupPaper();
    const api = http();
    for (let i = 0; i < 3; i++) {
      const r = await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } });
      expect(r.status).toBe(201);
    }
    const fourth = await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } });
    expect(fourth.status).toBe(409);
    expect(fourth.body.error.code).toBe('DAILY_LIMIT_REACHED');
  });

  test('concurrent starts cannot exceed the cap', async () => {
    const { student, paper } = await setupPaper();
    const api = http();
    const attempts = await Promise.all(
      Array.from({ length: 6 }, () =>
        api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })
      )
    );
    const created = attempts.filter((r) => r.status === 201);
    const rejected = attempts.filter((r) => r.status === 409);
    expect(created).toHaveLength(3);
    expect(rejected).toHaveLength(3);

    const dbCount = await Exam.count({ where: { userId: student.id, status: 'in_progress' } });
    expect(dbCount).toBe(3);
  });

  test('limit window follows the organization timezone midnight boundary', async () => {
    const org = await makeOrg('跨日组织', 'Asia/Shanghai');
    const sup = await makeUser(org.id, 'supervisor');
    const student = await makeUser(org.id, 'student');
    await seedQuestions(bankByDifficulty(8, 20));
    const api = http();
    const paper = (await api.post('/api/papers', { userId: sup.id, body: { seed: 'tz-boundary' } })).body;

    // 2026-03-08 23:30 Shanghai (15:30 UTC) — start 3 exams.
    const lateNight = '2026-03-08T15:30:00.000Z';
    for (let i = 0; i < 3; i++) {
      const r = await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id }, now: lateNight });
      expect(r.status).toBe(201);
    }
    expect((await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id }, now: lateNight })).status).toBe(409);

    // 2026-03-09 00:05 Shanghai (16:05 UTC) — new local day -> reset.
    const afterMidnight = '2026-03-08T16:05:00.000Z';
    const r = await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id }, now: afterMidnight });
    expect(r.status).toBe(201);
  });
});

describe('30-minute time limit', () => {
  test('submission exactly at deadline is on time; 1ms past is expired (0)', async () => {
    const { student, paper } = await setupPaper();
    const api = http();
    const t0 = '2026-05-01T00:00:00.000Z';
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id }, now: t0 })).body;
    const answers = buildAnswers(paper.questions, { correctCount: 20 });

    const atDeadline = (await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers }, requestId: 'at-deadline', now: start.deadlineAt,
    })).body;
    expect(atDeadline.status).toBe('submitted');
    expect(atDeadline.score).toBe(100);

    // Second student to verify strict past-deadline behavior.
    const s2 = await makeUser((await Organization.findByPk(student.organizationId)).id, 'student');
    const start2 = (await api.post('/api/exams', { userId: s2.id, body: { paperId: paper.id }, now: '2026-05-01T03:00:00.000Z' })).body;
    const oneMsLate = new Date(new Date(start2.deadlineAt).getTime() + 1).toISOString();
    const late = await api.post(`/api/exams/${start2.id}/submit`, {
      userId: s2.id, body: { answers }, requestId: 'late-req', now: oneMsLate,
    });
    expect(late.status).toBe(201);
    expect(late.body.timedOut).toBe(true);
    expect(late.body.status).toBe('expired');
    expect(late.body.score).toBe(0);
  });

  test('explicit timeout terminalizes once and is idempotent', async () => {
    const { student, paper } = await setupPaper();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id }, now: '2026-06-01T00:00:00.000Z' })).body;

    const before = await api.post(`/api/exams/${start.id}/timeout`, { userId: student.id, now: '2026-06-01T00:29:59.000Z' });
    expect(before.status).toBe(409);
    expect(before.body.error.code).toBe('NOT_DUE');

    const due = await api.post(`/api/exams/${start.id}/timeout`, { userId: student.id, now: '2026-06-01T00:30:00.000Z' });
    expect(due.status).toBe(200);
    expect(due.body.result.status).toBe('expired');
    expect(due.body.result.score).toBe(0);

    const again = await api.post(`/api/exams/${start.id}/timeout`, { userId: student.id, now: '2026-06-01T00:35:00.000Z' });
    expect(again.body.changed).toBe(false);

    const dbExam = await Exam.findByPk(start.id);
    expect(dbExam.status).toBe('expired');
  });

  test('submit and timeout racing produce exactly one terminal state', async () => {
    const { student, paper } = await setupPaper();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id }, now: '2026-07-01T00:00:00.000Z' })).body;
    const answers = buildAnswers(paper.questions, { correctCount: 20 });
    const late = '2026-07-01T00:31:00.000Z';

    const [submitRes, timeoutRes] = await Promise.all([
      api.post(`/api/exams/${start.id}/submit`, { userId: student.id, body: { answers }, requestId: 'race-submit', now: late }),
      api.post(`/api/exams/${start.id}/timeout`, { userId: student.id, now: late }),
    ]);

    // Whatever the interleaving, both describe the same single end state.
    const statuses = [submitRes.body.status || (submitRes.body.result && submitRes.body.result.status),
      timeoutRes.body.result ? timeoutRes.body.result.status : undefined].filter(Boolean);
    expect(statuses.length).toBeGreaterThan(0);
    expect(new Set(statuses).size).toBe(1);
    expect(['expired']).toContain([...new Set(statuses)][0]);

    const dbExam = await Exam.findByPk(start.id);
    expect(['submitted', 'expired']).toContain(dbExam.status);
    const scoreVersions = await sequelize.models.ScoreVersion.count({ where: { examId: start.id } });
    expect(scoreVersions).toBe(1);
  });
});

describe('idempotent grading', () => {
  test('same request id same content replays; different content conflicts', async () => {
    const { student, paper } = await setupPaper();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    const answers = buildAnswers(paper.questions, { correctCount: 20 });

    const first = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers }, requestId: 'idem-1',
    });
    expect(first.status).toBe(201);
    expect(first.body.score).toBe(100);

    const replay = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers }, requestId: 'idem-1',
    });
    expect(replay.status).toBe(200);
    expect(replay.body.idempotentReplay).toBe(true);
    expect(replay.body.score).toBe(100);

    const changed = JSON.parse(JSON.stringify(answers));
    changed['1'] = changed['1'][0] === 'A' ? ['B'] : ['A'];
    const clash = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers: changed }, requestId: 'idem-1',
    });
    expect(clash.status).toBe(409);
    expect(clash.body.error.code).toBe('IDEMPOTENCY_CONFLICT');
  });

  test('concurrent duplicate requests grade exactly once', async () => {
    const { student, paper } = await setupPaper();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    const answers = buildAnswers(paper.questions, { correctCount: 15 });

    const results = await Promise.all(
      Array.from({ length: 4 }, () =>
        api.post(`/api/exams/${start.id}/submit`, {
          userId: student.id, body: { answers }, requestId: 'concurrent-same',
        })
      )
    );
    const statuses = results.map((r) => r.status).sort();
    expect(statuses).toEqual([200, 200, 200, 201]);
    results.forEach((r) => expect(r.body.score).toBe(75));

    const versions = await sequelize.models.ScoreVersion.count({ where: { examId: start.id } });
    expect(versions).toBe(1);
    const submissions = await sequelize.models.Submission.count({ where: { examId: start.id } });
    expect(submissions).toBe(1);
  });

  test('a different request id after terminal state cannot create a second result', async () => {
    const { student, paper } = await setupPaper();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    const answers = buildAnswers(paper.questions, { correctCount: 20 });

    const first = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers }, requestId: 'first-id',
    });
    expect(first.status).toBe(201);

    const other = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers }, requestId: 'second-id',
    });
    expect(other.status).toBe(200);
    expect(other.body.alreadyTerminal).toBe(true);
    expect(other.body.score).toBe(100);

    expect(await sequelize.models.ScoreVersion.count({ where: { examId: start.id } })).toBe(1);
    const exam = await Exam.findByPk(start.id);
    expect(exam.status).toBe('submitted');
  });
});
