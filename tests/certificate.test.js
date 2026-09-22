'use strict';

const { app, request, createOrg, createUser, auth, seedBalancedQuestions } = require('./helpers');
const clock = require('../src/config/clock');

async function passExam(seed) {
  await createOrg(1, 'UTC');
  await createUser({ id: 1, role: 'admin', token: 'adm' });
  await createUser({ id: 2, role: 'student', token: 'stu' });
  await createUser({ id: 3, role: 'supervisor', token: 'sup' });
  await seedBalancedQuestions(1, 8);
  const paper = (await request(app).post('/api/papers').set(auth('adm')).send({ seed }).expect(201)).body.data;
  const exam = (await request(app).post(`/api/papers/${paper.id}/exams`).set(auth('stu')).expect(201)).body.data;
  const answers = {};
  paper.questions.forEach((q) => { answers[q.order] = q.answer; });
  const sub = await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu'))
    .send({ requestId: `${seed}-sub`, answers }).expect(201);
  return { paper, exam, sub };
}

describe('复核与成绩版本', () => {
  test('复核不改历史行，而是新增版本并记录原因；掌握度随之改变', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await passExam('rev1');
    expect((await request(app).get('/api/mastery').set(auth('stu'))).body.data.mastery).toBe(100);

    // 将第 1 题得分从 5 改为 0 => 百分制 95
    const r = await request(app).post(`/api/exams/${exam.sessionId}/reviews`).set(auth('sup'))
      .send({ reason: '复核发现第1题误判', adjustments: { 1: 0 } }).expect(200);
    expect(r.body.data.previous.version).toBe(1);
    expect(r.body.data.current.version).toBe(2);
    expect(r.body.data.current.score).toBe(95);
    expect(r.body.data.lowered).toBe(true);

    const res = (await request(app).get(`/api/exams/${exam.sessionId}/result`).set(auth('stu')).expect(200)).body.data;
    expect(res.score.version).toBe(2);
    expect(res.score.score).toBe(95);
    expect(res.score.reason).toMatch(/误判/);
    const mastery = (await request(app).get('/api/mastery').set(auth('stu'))).body.data;
    expect(mastery.basis.version).toBe(2);
    expect(mastery.mastery).toBe(95);
  });

  test('复核必须提供原因', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await passExam('rev2');
    await request(app).post(`/api/exams/${exam.sessionId}/reviews`).set(auth('sup'))
      .send({ adjustments: { 1: 0 } }).expect(400);
  });

  test('降分导致不满足门槛时，已发证书被吊销并记录原因', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await passExam('rev3');
    const cert = (await request(app).post(`/api/exams/${exam.sessionId}/certificate`).set(auth('stu')).expect(201)).body.data;
    expect(cert.effective).toBe(true);

    // 成绩 100 -> 75：跌破 80，证书失效
    const r = await request(app).post(`/api/exams/${exam.sessionId}/reviews`).set(auth('sup'))
      .send({ reason: '批量纠错', adjustments: { 1: 0, 2: 0, 3: 0, 4: 0, 5: 0 } }).expect(200);
    expect(r.body.data.current.score).toBe(75);
    expect(r.body.data.certificateAction).toBe('revoked');

    const mine = (await request(app).get('/api/certificates').set(auth('stu')).expect(200)).body.data[0];
    expect(mine.effectiveState).toBe('revoked');
    expect(mine.effective).toBe(false);
    expect(mine.revokedReason).toMatch(/批量纠错/);
  });
});

describe('掌握度的 30 天时间边界', () => {
  test('满 30 天同一时刻下降 5 分；第 29 天 23:59 不降；最低 0', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const {} = await passExam('decay1');

    clock.freeze('2026-10-21T23:59:00Z'); // 29 天 23:59
    expect((await request(app).get('/api/mastery').set(auth('stu'))).body.data.mastery).toBe(100);

    clock.freeze('2026-10-22T00:00:00Z'); // 恰好 30 天
    expect((await request(app).get('/api/mastery').set(auth('stu'))).body.data.mastery).toBe(95);

    // 登记学习活动后恢复（以最近活动重置计时）
    await request(app).post('/api/users/2/activities').set(auth('sup'))
      .send({ type: 'training', occurredAt: '2026-10-22T00:00:00Z' }).expect(201);
    expect((await request(app).get('/api/mastery').set(auth('stu'))).body.data.mastery).toBe(100);
  });
});

describe('证书发证规则', () => {
  test('掌握度>=85 且 当次>=80 才发证；有效期 12 个月；同一考试不重复发证', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await passExam('cert1');
    const cert = (await request(app).post(`/api/exams/${exam.sessionId}/certificate`).set(auth('stu')).expect(201)).body.data;
    expect(cert.issuedAt).toBe('2026-09-22T00:00:00.000Z');
    expect(cert.validUntil).toBe('2027-09-22T00:00:00.000Z'); // 12 个自然月
    expect(cert.effectiveState).toBe('valid');

    await request(app).post(`/api/exams/${exam.sessionId}/certificate`).set(auth('stu')).expect(409);

    // 12 个月边界：validUntil 整时刻即过期
    clock.freeze('2027-09-21T23:59:59.999Z');
    expect((await request(app).get('/api/certificates').set(auth('stu'))).body.data[0].effective).toBe(true);
    clock.freeze('2027-09-22T00:00:00.000Z');
    expect((await request(app).get('/api/certificates').set(auth('stu'))).body.data[0].effectiveState).toBe('expired');
  });

  test('当次 75 分不予发证（即使掌握度规则满足）', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    await createOrg(1, 'UTC');
    await createUser({ id: 1, role: 'admin', token: 'adm' });
    await createUser({ id: 2, role: 'student', token: 'stu' });
    await seedBalancedQuestions(1, 8);
    const paper = (await request(app).post('/api/papers').set(auth('adm')).send({ seed: 'cert2' }).expect(201)).body.data;
    const exam = (await request(app).post(`/api/papers/${paper.id}/exams`).set(auth('stu')).expect(201)).body.data;
    // 只答对 15 题 => 75 分
    const answers = {};
    paper.questions.slice(0, 15).forEach((q) => { answers[q.order] = q.answer; });
    await request(app).post(`/api/exams/${exam.sessionId}/submit`).set(auth('stu'))
      .send({ requestId: 'cert2-sub', answers }).expect(201);
    const r = await request(app).post(`/api/exams/${exam.sessionId}/certificate`).set(auth('stu')).expect(400);
    expect(r.body.error.message).toMatch(/当次成绩 75/);
  });

  test('主管可按组织查询证书与学员掌握度；跨组织不可见', async () => {
    clock.freeze('2026-09-22T00:00:00Z');
    const { exam } = await passExam('cert3');
    await request(app).post(`/api/exams/${exam.sessionId}/certificate`).set(auth('stu')).expect(201);
    const list = await request(app).get('/api/org/certificates').set(auth('sup')).expect(200);
    expect(list.body.data).toHaveLength(1);
    const mastery = await request(app).get('/api/users/2/mastery').set(auth('sup')).expect(200);
    expect(mastery.body.data.mastery).toBe(100);

    await createOrg(2, 'UTC');
    await createUser({ id: 9, orgId: 2, role: 'supervisor', token: 'sup2' });
    await request(app).get('/api/org/certificates').set(auth('sup2')).expect(200);
    const cross = await request(app).get('/api/org/certificates').set(auth('sup2'));
    expect(cross.body.data).toHaveLength(0);
  });
});
