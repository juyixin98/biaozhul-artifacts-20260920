const {
  sequelize,
  truncateAll,
  makeOrg,
  makeUser,
  seedQuestions,
  bankByDifficulty,
  http,
} = require('./helpers');

beforeAll(async () => {
  await sequelize.authenticate();
});
beforeEach(truncateAll);
afterAll(async () => sequelize.close());

async function setup() {
  const org = await makeOrg('A');
  const sup = await makeUser(org.id, 'supervisor');
  const student = await makeUser(org.id, 'student');
  await seedQuestions(bankByDifficulty(8, 20));
  const paper = (await http().post('/api/papers', { userId: sup.id, body: { seed: 'grading-paper' } })).body;
  return { org, sup, student, paper };
}

function answersFor(paper, mutate) {
  const answers = {};
  for (const q of paper.questions) answers[q.position] = q.answer.slice();
  if (mutate) mutate(answers, paper);
  return answers;
}

describe('answer isolation before submission', () => {
  test('taking view exposes stems/options but never answers or explanations', async () => {
    const { student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    const raw = JSON.stringify(start);
    expect(raw).not.toMatch(/explanation/);
    expect(start.questions.some((q) => 'answer' in q)).toBe(false);

    const getView = (await api.get(`/api/exams/${start.id}`, { userId: student.id })).body;
    expect(getView.questions.some((q) => 'answer' in q)).toBe(false);

    // Result while still in progress does not leak answers either.
    const pending = await api.get(`/api/exams/${start.id}/result`, { userId: student.id });
    expect(pending.body.status).toBe('in_progress');
    expect(JSON.stringify(pending.body)).not.toMatch(/correctAnswer/);
  });

  test('student has no access to bank endpoints that carry answers', async () => {
    const { student } = await setup();
    const r = await http().get('/api/questions', { userId: student.id });
    expect(r.status).toBe(403);
  });
});

describe('server-side grading rules', () => {
  test('multiple choice must match exactly: partial selection scores 0', async () => {
    const { student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    const multis = paper.questions.filter((q) => q.type === 'multiple');
    expect(multis.length).toBeGreaterThan(0);

    const answers = answersFor(paper, (a) => {
      for (const q of multis) a[q.position] = [q.answer[0]]; // only one of N correct
    });
    const r = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers }, requestId: 'partial-multi',
    });
    expect(r.status).toBe(201);
    expect(r.body.score).toBe(100 - multis.length * 5);
    for (const d of r.body.detail.filter((x) => x.type === 'multiple')) {
      expect(d.pointsEarned).toBe(0);
    }

    // A wrong EXTRA option on a multi also scores zero.
    const student2 = await makeUser((await sequelize.models.Organization.findByPk(student.organizationId)).id, 'student');
    const start2 = (await api.post('/api/exams', { userId: student2.id, body: { paperId: paper.id } })).body;
    const answers2 = answersFor(paper, (a) => {
      const q = multis[0];
      a[q.position] = [...q.answer, q.options.find((_, i) => !q.answer.includes(String.fromCharCode(65 + i))) ? 'D' : 'C'];
    });
    // Guarantee at least one genuinely extra option.
    const target = multis[0];
    const extra = target.options.map((_, i) => String.fromCharCode(65 + i)).find((o) => !target.answer.includes(o));
    answers2[target.position] = [...target.answer, extra];
    const r2 = await api.post(`/api/exams/${start2.id}/submit`, {
      userId: student2.id, body: { answers: answers2 }, requestId: 'extra-multi',
    });
    expect(r2.body.detail.find((d) => d.position === target.position).pointsEarned).toBe(0);
  });

  test('boolean questions grade on exact true/false set', async () => {
    const { student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    const bools = paper.questions.filter((q) => q.type === 'boolean');
    expect(bools.length).toBeGreaterThan(0);

    const answers = answersFor(paper, (a) => {
      for (const q of bools) a[q.position] = [q.answer[0] === 'true' ? 'false' : 'true'];
    });
    const r = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers }, requestId: 'bool-wrong',
    });
    expect(r.body.detail.filter((d) => d.type === 'boolean').every((d) => !d.correct)).toBe(true);
  });

  test('graded result reveals answers/explanations only after grading; wrong questions use snapshot', async () => {
    const { student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;
    const answers = answersFor(paper, (a) => { a['1'] = ['__wrong__']; });
    const graded = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers }, requestId: 'grade-view',
    });
    expect(graded.body.detail[0].correctAnswer).toEqual(paper.questions[0].answer);
    expect(typeof graded.body.detail[0].explanation).toBe('string');

    const wrong = await api.get(`/api/exams/${start.id}/wrong-questions`, { userId: student.id });
    expect(wrong.body.wrongQuestions.some((w) => w.stem === paper.questions[0].stem)).toBe(true);
    expect(wrong.body.wrongQuestions[0].correctAnswer).toEqual(paper.questions[0].answer);
  });

  test('late submission scores 0 and is recorded as expired (no grading leak advantage)', async () => {
    const { student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id }, now: '2026-01-10T10:00:00.000Z' })).body;
    const answers = answersFor(paper);
    const late = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers }, requestId: 'too-late', now: '2026-01-10T10:30:01.000Z',
    });
    expect(late.body.status).toBe('expired');
    expect(late.body.score).toBe(0);
    expect(late.body.correctCount).toBe(0);
  });
});

describe('frozen version used for grading and wrong-question review', () => {
  test('changing the bank after the exam started does not change what is graded', async () => {
    const { sup, student, paper } = await setup();
    const api = http();
    const start = (await api.post('/api/exams', { userId: student.id, body: { paperId: paper.id } })).body;

    const { Question } = require('../src/models');
    // Swap all bank answers while the exam is in progress.
    const bank = await Question.findAll();
    for (const q of bank) {
      q.answer = q.type === 'boolean' ? [q.answer[0] === 'true' ? 'false' : 'true']
        : [q.answer[0] === 'A' ? 'B' : 'A'];
      await q.save();
    }

    const answers = {};
    for (const q of paper.questions) answers[q.position] = q.answer;
    const r = await api.post(`/api/exams/${start.id}/submit`, {
      userId: student.id, body: { answers }, requestId: 'graded-vs-frozen',
    });
    expect(r.body.score).toBe(100); // answers from the frozen paper snapshot

    // Supervisor paper view still shows the original frozen content.
    const frozenView = await api.get(`/api/papers/${paper.id}`, { userId: sup.id });
    expect(frozenView.body.questions.map((q) => q.answer)).toEqual(
      expect.arrayContaining([paper.questions[0].answer])
    );
  });
});
