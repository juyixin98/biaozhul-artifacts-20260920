'use strict';

const { app, request, createOrg, createUser, auth, seedBalancedQuestions } = require('./helpers');
const clock = require('../src/config/clock');

async function makePaper(adminToken = 'adm', seed = 's') {
  const r = await request(app).post('/api/papers').set(auth(adminToken)).send({ seed }).expect(201);
  return r.body.data.id;
}

describe('开考次数限制（组织时区每日 3 次）', () => {
  beforeEach(async () => {
    await createOrg(1, 'Asia/Shanghai'); // UTC+8
    await createUser({ id: 1, role: 'admin', token: 'adm' });
    await createUser({ id: 2, role: 'student', token: 'stu' });
    await seedBalancedQuestions(1, 8);
  });

  const start = (paperId, token = 'stu') =>
    request(app).post(`/api/papers/${paperId}/exams`).set(auth(token));

  test('每日第 4 次开始被拒绝，并返回组织时区日界', async () => {
    clock.freeze('2026-09-22T01:00:00Z'); // 上海 09:00
    const paperId = await makePaper('adm', 'p1');
    for (let i = 0; i < 3; i += 1) await start(paperId).expect(201);
    const r4 = await start(paperId).expect(403);
    expect(r4.body.error.message).toMatch(/上限 3 次/);
    expect(r4.body.error.message).toMatch('2026-09-21T16:00:00.000Z'); // 上海当日 00:00 = 前一日 16:00Z
  });

  test('跨组织时区日界（上海 00:00，即 UTC16:00）后次数重置', async () => {
    const paperId = await makePaper('adm', 'p2');
    clock.freeze('2026-09-22T15:59:00Z'); // 上海 23:59
    await start(paperId).expect(201);
    await start(paperId).expect(201);
    await start(paperId).expect(201);
    await start(paperId).expect(403);

    clock.freeze('2026-09-22T16:00:00Z'); // 上海次日 00:00 —— 新的自然日
    const ok = await start(paperId).expect(201);
    expect(ok.body.data.dailyUsed).toBe(1);
  });

  test('并发开考不能突破 3 次上限（10 个并发请求仅 3 个成功）', async () => {
    clock.freeze('2026-09-22T02:00:00Z');
    const paperId = await makePaper('adm', 'p3');
    const results = await Promise.all(Array.from({ length: 10 }, () => start(paperId)));
    const created = results.filter((r) => r.status === 201);
    const denied = results.filter((r) => r.status === 403);
    expect(created).toHaveLength(3);
    expect(denied).toHaveLength(7);
  }, 30000);

  test('学员不能查询他人组织/跨学员的数据隔离（学员列表接口禁用）', async () => {
    const paperId = await makePaper('adm', 'p4');
    await start(paperId).expect(201);
    await request(app).get('/api/exams').set(auth('stu')).expect(403);
  });
});
