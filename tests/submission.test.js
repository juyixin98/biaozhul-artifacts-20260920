'use strict';

const { app, request, createOrg, createUser, auth, seedBalancedQuestions } = require('./helpers');
const clock = require('../src/config/clock');

async function setup(seed = 'g') {
  await createOrg(1, 'UTC');
  await createUser({ id: 1, role: 'admin', token: 'adm' });
  await createUser({ id: 2, role: 'student', token: 'stu' });
  await createUser({ id: 3, role: 'supervisor', token: 'sup' });
  await seedBalancedQuestions(1, 8);
  const paper = (await request(app).post('/api/papers').set(auth('adm')).send({ seed }).expect(201)).body.data;
  const exam = (await request(app).post(`/api/papers/${paper.id}/exams`).set(auth('stu')).expect(201)).body.data;
  return { paper, exam };
}

describe('服务端评分与考试终态', () => {
  test('全对 100 分；多选少选/多选不得分', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { paper, exam } = await setup('grade1');
    const correct = {};
    paper.questions.forEach((q) => {
      correct[q.order] = q.answer; // 来自管理视图的答案
    });
    // 把其中一道多选题改成“少选一个”，该题 0 分
    const multi = paper.questions.find((q) => q.type === 'multiple');
    correct[multi.order] = ['A'];

    const r = await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu'))
      .send({ requestId: 'r-1', answers: correct }).expect(201);
    expect(r.body.data.score).toBe(95); // 20 题每题 5 分，错一题 => 95
    expect(r.body.idempotent).toBe(false);
  });

  test('同 requestId 同内容重复提交返回原成绩（幂等）', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await setup('idem1');
    const payload = { requestId: 'dup-1', answers: { 1: 'A' } };
    const first = await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu')).send(payload).expect(201);
    const second = await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu')).send(payload).expect(200);
    expect(second.body.idempotent).toBe(true);
    expect(second.body.data.scoreVersionId).toBe(first.body.data.scoreVersionId);
    expect(second.body.data.score).toBe(first.body.data.score);
  });

  test('同 requestId 不同内容报 409 冲突', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await setup('conf1');
    await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu'))
      .send({ requestId: 'same-id', answers: { 1: 'A' } }).expect(201);
    const r = await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu'))
      .send({ requestId: 'same-id', answers: { 1: 'B' } }).expect(409);
    expect(r.body.error.code).toBe('CONFLICT');
  });

  test('新 requestId 的第二次交卷被拒：终态唯一', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await setup('term1');
    await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu'))
      .send({ requestId: 'x1', answers: {} }).expect(201);
    await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu'))
      .send({ requestId: 'x2', answers: {} }).expect(409);
  });

  test('截止边界：t == deadline(30:00) 视为超时，按已提交答案判分且 finalReason=timeout', async () => {
    const t0 = '2026-09-22T00:00:00.000Z';
    clock.freeze(t0);
    const { paper, exam } = await setup('edge1');
    expect(exam.deadlineAt).toBe('2026-09-22T00:30:00.000Z');

    // 29:59.999 仍在窗口
    clock.freeze('2026-09-22T00:29:59.999Z');
    const correct = {};
    paper.questions.forEach((q) => { correct[q.order] = q.answer; });
    const early = await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu'))
      .send({ requestId: 'just-in-time', answers: {} }).expect(201);
    expect(early.body.data.late).toBe(false);
  });

  test('超过 deadline 的交卷记 late/timeout，但只产生一个成绩版本', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await setup('late1');
    clock.freeze('2026-09-22T00:30:00.000Z'); // 恰为截止时刻 => 超时
    const r = await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu'))
      .send({ requestId: 'late-req', answers: {} }).expect(201);
    expect(r.body.data.late).toBe(true);

    const res = (await request(app).get(`/api/exams/${exam.sessionId}/result`).set(auth('stu')).expect(200)).body.data;
    expect(res.finalReason).toBe('timeout');
    expect(res.score.score).toBe(0);
    expect(res.score.version).toBe(1);
  });

  test('超时扫描将未交卷场次判 0 分；扫描与交卷并发不会产生重复终态', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await setup('sweep1');
    clock.freeze('2026-09-22T00:31:00Z');
    // 主管触发扫描
    const swept = await request(app).post('/api/exams/sweep-timeouts').set(auth('sup')).expect(200);
    expect(swept.body.data.finalized.map((f) => f.sessionId)).toContain(exam.sessionId);
    // 再次扫描幂等，无新增
    const again = await request(app).post('/api/exams/sweep-timeouts').set(auth('sup')).expect(200);
    expect(again.body.data.finalized.map((f) => f.sessionId)).not.toContain(exam.sessionId);

    const res = (await request(app).get(`/api/exams/${exam.sessionId}/result`).set(auth('stu')).expect(200)).body.data;
    expect(res.status).toBe('graded');
    expect(res.finalReason).toBe('timeout');
  });

  test('提交与超时扫描并发：只有一个终态、一个成绩版本', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await setup('race1');
    clock.freeze('2026-09-22T00:30:05Z');
    const [submitRes, sweepRes] = await Promise.all([
      request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu')).send({ requestId: 'race-req', answers: {} }),
      request(app).post('/api/exams/sweep-timeouts').set(auth('sup')),
    ]);
    expect([201, 409]).toContain(submitRes.status);
    expect(sweepRes.status).toBe(200);
    const res = (await request(app).get(`/api/exams/${exam.sessionId}/result`).set(auth('stu')).expect(200)).body.data;
    expect(res.status).toBe('graded');
    expect(res.score.version).toBe(1); // 仅有一个版本
  }, 20000);

  test('未出分前 result 不泄露答案；出分后可看错题（含冻结版本与解析）', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await setup('leak1');
    const pending = (await request(app).get(`/api/exams/${exam.sessionId}/result`).set(auth('stu')).expect(200)).body.data;
    expect(pending.status).toBe('in_progress');
    expect(JSON.stringify(pending)).not.toMatch(/explanation/);

    await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu'))
      .send({ requestId: 'leak-req', answers: {} }).expect(201);
    const graded = (await request(app).get(`/api/exams/${exam.sessionId}/result`).set(auth('stu')).expect(200)).body.data;
    expect(graded.wrongQuestions.length).toBeGreaterThan(0);
    expect(graded.wrongQuestions[0]).toHaveProperty('explanation'); // 错题保存所用版本
    expect(graded.wrongQuestions[0]).toHaveProperty('stem');
  });

  test('学员不能查看他人考试', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await setup('acl1');
    await createUser({ id: 4, orgId: 1, role: 'student', token: 'other' });
    await request(app).get(`/api/exams/${exam.sessionId}/result`).set(auth('other')).expect(403);
  });
});
