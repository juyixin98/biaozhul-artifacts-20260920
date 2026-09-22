'use strict';

// 端到端演示脚本：组卷 -> 开考 -> 全对交卷 -> 掌握度 -> 发证，全程使用可控时钟。
const { sequelize } = require('./index');
const clock = require('../config/clock');
const paperService = require('../services/paperService');
const examService = require('../services/examService');
const masteryService = require('../services/masteryService');
const certService = require('../services/certificateService');
const { User } = require('./index');

async function main() {
  clock.freeze('2026-09-22T01:00:00Z'); // 上海时间 09:00，柏林 03:00（CET, UTC+2）
  const student = await User.findByPk(101, { include: [{ association: 'organization' }] });

  console.log('\n=== 1) 确定性组卷（seed=demo-2026-09） ===');
  const paper = await paperService.generatePaper(student.orgId, 'demo-2026-09', '9 月护理能力考核');
  const hard = paper.questions.filter((q) => q.difficulty >= 4).length;
  console.log(`试卷 #${paper.id}：${paper.questions.length} 题，难度>=4 共 ${hard} 题（要求 >=6）`);

  console.log('\n=== 2) 开始考试（30 分钟窗口、每日 3 次） ===');
  const started = await examService.startExam(student, paper.id);
  console.log('开考：', started);

  console.log('\n=== 3) 全对交卷（服务端评分；多选须完全一致） ===');
  const answers = {};
  paper.questions.forEach((q) => { answers[q.order] = q.answer; }); // 仅演示，答案在服务端比对
  const submitted = await examService.submit(student, started.sessionId, {
    requestId: 'demo-req-0001', answers,
  });
  console.log('成绩：', submitted.result);

  console.log('\n=== 4) 掌握度 ===');
  const masteryNow = await masteryService.getMastery(student.id);
  console.log(masteryNow);

  console.log('\n=== 5) 30 天无学习后的掌握度（边界：满 30 天同一时刻 -5） ===');
  clock.tick(30 * 86400000);
  const masteryLater = await masteryService.getMastery(student.id);
  console.log({ mastery: masteryLater.mastery, rule: masteryLater.rule, decayBoundary: masteryLater.decayBoundary });

  console.log('\n=== 6) 发证（掌握度/成绩双门槛，12 个月有效） ===');
  clock.tick(-30 * 86400000); // 回到出分当天申领
  const cert = await certService.issue(student, started.sessionId);
  console.log(cert);

  await sequelize.close();
  console.log('\n演示完成。');
}

main().catch((err) => { console.error('演示失败：', err); process.exit(1); });
