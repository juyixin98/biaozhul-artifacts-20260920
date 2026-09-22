'use strict';

const { app, request, createOrg, createUser, auth, seedQuestions, seedBalancedQuestions } = require('./helpers');

describe('确定性组卷与题目冻结', () => {
  beforeEach(async () => {
    await createOrg(1, 'Asia/Shanghai');
    await createUser({ id: 1, role: 'admin', token: 'adm' });
    await createUser({ id: 2, role: 'student', token: 'stu' });
  });

  test('同种子两次组卷结果完全一致，且 20 题中难度>=4 至少 6 道', async () => {
    await seedBalancedQuestions(1, 8); // 40 题，难度 1–5 各 8
    const r1 = await request(app).post('/api/papers').set(auth('adm')).send({ seed: 's1' }).expect(201);
    const r2 = await request(app).post('/api/papers').set(auth('adm')).send({ seed: 's1' }).expect(201);

    const sig = (p) => p.data.questions.map((q) => `${q.order}:${q.stem}`).join('|');
    expect(r1.body.data.id).toBe(r2.body.data.id); // 幂等：同一份试卷
    expect(sig(r1.body)).toBe(sig(r2.body));
    expect(r1.body.data.questions).toHaveLength(20);
    const hard = r1.body.data.questions.filter((q) => q.difficulty >= 4);
    expect(hard.length).toBeGreaterThanOrEqual(6);
  });

  test('不同种子产生不同抽题/排序', async () => {
    await seedBalancedQuestions(1, 8);
    const a = await request(app).post('/api/papers').set(auth('adm')).send({ seed: 'alpha' }).expect(201);
    const b = await request(app).post('/api/papers').set(auth('adm')).send({ seed: 'beta' }).expect(201);
    const sa = a.body.data.questions.map((q) => q.stem).join('|');
    const sb = b.body.data.questions.map((q) => q.stem).join('|');
    expect(sa).not.toBe(sb);
  });

  test('题量不足：明确失败，不重复凑数', async () => {
    await seedQuestions(1, Array.from({ length: 19 }, (_, i) => ({ difficulty: (i % 5) + 1 })));
    const r = await request(app).post('/api/papers').set(auth('adm')).send({ seed: 'small' }).expect(400);
    expect(r.body.error.message).toMatch(/题库题目不足/);
  });

  test('高难度题不足：明确失败', async () => {
    // 20 题但只有 5 道难度>=4（要求 6）
    const specs = Array.from({ length: 20 }, (_, i) => ({ difficulty: i < 5 ? 4 : 2 }));
    await seedQuestions(1, specs);
    const r = await request(app).post('/api/papers').set(auth('adm')).send({ seed: 'hard' }).expect(400);
    expect(r.body.error.message).toMatch(/高难度题目不足/);
  });

  test('冻结：组卷后编辑题库，试卷快照（题干/答案/解析）不变', async () => {
    await seedBalancedQuestions(1, 8);
    const paper = (await request(app).post('/api/papers').set(auth('adm')).send({ seed: 'freeze' }).expect(201)).body.data;
    const first = paper.questions.find((q) => q.type === 'single');

    // 管理员修改被抽中的题目（只改 stem/answer/explanation）
    await request(app).patch(`/api/questions/${first.questionId}`).set(auth('adm'))
      .send({ stem: '题干已被修改', answer: 'B', explanation: '解析已改' }).expect(200);

    const refetched = (await request(app).get(`/api/papers/${paper.id}/full`).set(auth('adm')).expect(200)).body.data;
    const still = refetched.questions.find((q) => q.questionId === first.questionId);
    expect(still.stem).toBe(first.stem);
    expect(still.answer).toBe(first.answer);
    expect(still.explanation).toBe(first.explanation);
  });

  test('答案隔离：学员视图不包含 answer/explanation，管理视图包含', async () => {
    await seedBalancedQuestions(1, 8);
    const paper = (await request(app).post('/api/papers').set(auth('adm')).send({ seed: 'iso' }).expect(201)).body.data;

    const stu = (await request(app).get(`/api/papers/${paper.id}`).set(auth('stu')).expect(200)).body.data;
    expect(JSON.stringify(stu.questions)).not.toMatch(/"answer"/);
    expect(JSON.stringify(stu.questions)).not.toMatch(/解析/);

    const adm = (await request(app).get(`/api/papers/${paper.id}/full`).set(auth('adm')).expect(200)).body.data;
    expect(adm.questions[0]).toHaveProperty('answer');
    expect(adm.questions[0]).toHaveProperty('explanation');
  });

  test('学员不能访问题库管理接口', async () => {
    await request(app).get('/api/questions').set(auth('stu')).expect(403);
  });
});
