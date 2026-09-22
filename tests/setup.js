'use strict';

// Jest 全局夹具：每个测试文件前重置时钟、截断业务表；全部迁移完成后运行用例。
const { sequelize } = require('../src/db');
const umzug = require('../src/db/migrate');
const clock = require('../src/config/clock');

const TABLES = [
  'learning_activities', 'certificates', 'score_versions', 'submissions',
  'exam_sessions', 'paper_questions', 'papers', 'questions', 'users', 'organizations',
];

let migrated = false;

beforeAll(async () => {
  if (!migrated) {
    await sequelize.authenticate();
    await umzug.up();
    migrated = true;
  }
});

beforeEach(async () => {
  clock.reset();
  // 按外键依赖逆序截断
  await sequelize.query('SET FOREIGN_KEY_CHECKS=0');
  for (const t of TABLES) {
    // eslint-disable-next-line no-await-in-loop
    await sequelize.query(`TRUNCATE TABLE ${t}`);
  }
  await sequelize.query('SET FOREIGN_KEY_CHECKS=1');
});

afterAll(async () => {
  await sequelize.close();
});
