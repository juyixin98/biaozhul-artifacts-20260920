'use strict';

// 幂等播种：2 个组织（不同时区）、各角色用户、40 道覆盖三题型与 1–5 难度的题。
const { sequelize, Organization, User, Question } = require('./index');

const ORGS = [
  { id: 1, name: 'CareOps 华东护理中心', timezone: 'Asia/Shanghai' },
  { id: 2, name: 'CareOps Berlin Hub', timezone: 'Europe/Berlin' },
];

const USERS = [
  { id: 101, orgId: 1, name: '张学员', role: 'student', apiToken: 'token-stu-101' },
  { id: 102, orgId: 1, name: '李主管', role: 'supervisor', apiToken: 'token-sup-102' },
  { id: 103, orgId: 1, name: '王管理员', role: 'admin', apiToken: 'token-adm-103' },
  { id: 201, orgId: 2, name: 'Anna Schüler', role: 'student', apiToken: 'token-stu-201' },
  { id: 202, orgId: 2, name: 'Eva Supervisor', role: 'supervisor', apiToken: 'token-sup-202' },
];

function buildQuestions(orgId) {
  const qs = [];
  const letters = ['A', 'B', 'C', 'D'];
  for (let i = 1; i <= 40; i += 1) {
    const difficulty = ((i - 1) % 5) + 1; // 均匀覆盖 1–5
    const type = i % 3 === 0 ? 'multiple' : i % 3 === 1 ? 'single' : 'judge';
    if (type === 'judge') {
      qs.push({
        orgId, difficulty, type,
        stem: `[${orgId}-${i}] 护理规范判断：操作前必须执行手卫生并核对患者身份（难度${difficulty}）`,
        options: null,
        answer: true,
        explanation: '手卫生与双标识核对是感控与患者安全基线要求。',
        points: 5,
      });
    } else if (type === 'single') {
      qs.push({
        orgId, difficulty, type,
        stem: `[${orgId}-${i}] 单选题：成人心肺复苏按压深度应为多少（难度${difficulty}）？`,
        options: letters.map((k) => ({ key: k, text: { A: '2–3 cm', B: '5–6 cm', C: '8–10 cm', D: '越深越好' }[k] })),
        answer: 'B',
        explanation: '指南推荐成人胸外按压深度 5–6 cm。',
        points: 5,
      });
    } else {
      qs.push({
        orgId, difficulty, type,
        stem: `[${orgId}-${i}] 多选题：以下哪些属于标准预防措施（难度${difficulty}）？`,
        options: letters.map((k) => ({ key: k, text: {
          A: '手卫生', B: '个人防护用品', C: '呼吸卫生/咳嗽礼仪', D: '用饮料瓶盛装消毒液' }[k] })),
        answer: ['A', 'B', 'C'],
        explanation: 'D 违规：消毒剂须使用原包装或合规标识容器。',
        points: 5,
      });
    }
  }
  return qs;
}

async function seed() {
  for (const org of ORGS) {
    await Organization.findOrCreate({ where: { id: org.id }, defaults: org });
  }
  for (const u of USERS) {
    await User.findOrCreate({ where: { id: u.id }, defaults: u });
  }
  for (const org of ORGS) {
    const count = await Question.count({ where: { orgId: org.id } });
    if (count === 0) {
      await Question.bulkCreate(buildQuestions(org.id));
    }
  }
  const qCount = await Question.count();
  console.log(`播种完成：${ORGS.length} 组织、${USERS.length} 用户、${qCount} 题目`);
}

if (require.main === module) {
  seed().then(() => sequelize.close()).catch((err) => { console.error(err); process.exit(1); });
}

module.exports = { seed, ORGS, USERS };
