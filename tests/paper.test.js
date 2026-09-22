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

describe('deterministic paper composition', () => {
  test('same seed produces the identical paper; >=30% questions difficulty>=4', async () => {
    const org = await makeOrg('A');
    const sup = await makeUser(org.id, 'supervisor');
    await seedQuestions(bankByDifficulty(12, 20));

    const api = http();
    const r1 = await api.post('/api/papers', { userId: sup.id, body: { seed: 'fixed-seed-1' } });
    expect(r1.status).toBe(201);
    expect(r1.body.questions).toHaveLength(20);
    const hard1 = r1.body.questions.filter((q) => q.difficulty >= 4);
    expect(hard1.length).toBeGreaterThanOrEqual(6);

    const r2 = await api.post('/api/papers', { userId: sup.id, body: { seed: 'fixed-seed-1' } });
    expect(r2.status).toBe(201);
    const ids1 = r1.body.questions.map((q) => q.sourceQuestionId);
    const ids2 = r2.body.questions.map((q) => q.sourceQuestionId);
    expect(ids2).toEqual(ids1);
    expect(new Set(ids1).size).toBe(20); // no duplicated padding
  });

  test('different seeds yield (practically) different selections', async () => {
    const org = await makeOrg('A');
    const sup = await makeUser(org.id, 'supervisor');
    await seedQuestions(bankByDifficulty(12, 20));

    const api = http();
    const r1 = await api.post('/api/papers', { userId: sup.id, body: { seed: 'seed-X' } });
    const r2 = await api.post('/api/papers', { userId: sup.id, body: { seed: 'seed-Y' } });
    const ids1 = r1.body.questions.map((q) => q.sourceQuestionId);
    const ids2 = r2.body.questions.map((q) => q.sourceQuestionId);
    expect(ids2).not.toEqual(ids1);
  });

  test('fails explicitly when bank has fewer than 20 questions (no duplication)', async () => {
    const org = await makeOrg('A');
    const sup = await makeUser(org.id, 'supervisor');
    await seedQuestions(bankByDifficulty(8, 8)); // only 16

    const r = await http().post('/api/papers', { userId: sup.id, body: { seed: 'small-bank' } });
    expect(r.status).toBe(422);
    expect(r.body.error.code).toBe('INSUFFICIENT_QUESTIONS');
    expect(r.body.error.message).toMatch(/only 16/);
  });

  test('fails explicitly when hard questions are insufficient for 30% quota', async () => {
    const org = await makeOrg('A');
    const sup = await makeUser(org.id, 'supervisor');
    await seedQuestions(bankByDifficulty(3, 25)); // 28 total but only 3 hard

    const r = await http().post('/api/papers', { userId: sup.id, body: { seed: 'few-hard' } });
    expect(r.status).toBe(422);
    expect(r.body.error.code).toBe('INSUFFICIENT_HARD_QUESTIONS');
    expect(r.body.error.message).toMatch(/at least 6/);
  });

  test('paper is frozen: editing the bank cannot change stem/answer/explanation', async () => {
    const org = await makeOrg('A');
    const sup = await makeUser(org.id, 'supervisor');
    await seedQuestions(
      bankByDifficulty(6, 14).map((q, i) => ({ ...q, stem: `原始题干-${i}` }))
    );

    const api = http();
    const paper = (await api.post('/api/papers', { userId: sup.id, body: { seed: 'freeze-check' } })).body;
    const first = paper.questions[0];

    // Edit every live bank question.
    const { Question } = require('../src/models');
    await Question.update(
      { stem: '被修改的题干', answer: ['B'], explanation: '被修改的解析', active: false },
      { where: {} }
    );

    const view = (await api.get(`/api/papers/${paper.id}`, { userId: sup.id })).body;
    const frozen = view.questions.find((q) => q.paperQuestionId === first.paperQuestionId);
    expect(frozen.stem).toBe(first.stem);
    expect(frozen.answer).toEqual(first.answer);
    expect(frozen.explanation).toBe(first.explanation);
    expect(frozen.stem).not.toBe('被修改的题干');

    // A new paper cannot use deactivated questions (bank state changed).
    const regen = await api.post('/api/papers', { userId: sup.id, body: { seed: 'after-bank-change' } });
    expect(regen.status).toBe(422);
    expect(regen.body.error.code).toBe('INSUFFICIENT_QUESTIONS');
  });
});
