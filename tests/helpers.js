const request = require('supertest');
const app = require('../src/app');
const {
  sequelize,
  Organization,
  User,
  Question,
} = require('../src/models');

// ---- DB reset ----
async function truncateAll() {
  await sequelize.query('SET FOREIGN_KEY_CHECKS = 0');
  const rows = await sequelize.query(`
    SELECT TABLE_NAME FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = :db AND TABLE_NAME <> 'schema_migrations'
  `, { replacements: { db: sequelize.config.database } });
  for (const row of rows[0]) {
    await sequelize.query(`TRUNCATE TABLE \`${row.TABLE_NAME}\``);
  }
  await sequelize.query('SET FOREIGN_KEY_CHECKS = 1');
}

// ---- Factories ----
async function makeOrg(name = '测试组织', timezone = 'Asia/Shanghai') {
  return Organization.create({ name: `${name}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`, timezone });
}

async function makeUser(orgId, role = 'student', name) {
  return User.create({
    organizationId: orgId ?? null,
    role,
    name: name || `${role}-${Math.random().toString(36).slice(2, 8)}`,
  });
}

const LETTERS = ['A', 'B', 'C', 'D'];

function makeQuestion(overrides = {}) {
  const type = overrides.type || 'single';
  if (type === 'boolean') {
    return {
      type: 'boolean',
      difficulty: overrides.difficulty ?? 2,
      stem: overrides.stem || `判断题-${Math.random()}`,
      options: ['true', 'false'],
      answer: overrides.answer || ['true'],
      explanation: '解析',
      organizationId: overrides.organizationId ?? null,
    };
  }
  const options = overrides.options || ['选项A', '选项B', '选项C', '选项D'];
  return {
    type,
    difficulty: overrides.difficulty ?? 2,
    stem: overrides.stem || `题干-${Math.random()}`,
    options,
    answer: overrides.answer || (type === 'multiple' ? ['A', 'B'] : ['A']),
    explanation: overrides.explanation ?? '解析',
    organizationId: overrides.organizationId ?? null,
  };
}

async function seedQuestions(specs, organizationId = null) {
  return Question.bulkCreate(specs.map((s) => makeQuestion({ ...s, organizationId })));
}

// Build a bank of exactly the requested difficulty distribution.
function bankByDifficulty(hard, easy) {
  const specs = [];
  for (let i = 0; i < hard; i++) {
    const type = i % 3 === 0 ? 'multiple' : i % 3 === 1 ? 'single' : 'boolean';
    specs.push({
      type,
      difficulty: 4 + (i % 2),
      // Multiple-choice questions must carry at least two correct options.
      ...(type === 'multiple' ? { answer: ['A', 'B'] } : {}),
    });
  }
  for (let i = 0; i < easy; i++) {
    const type = i % 3 === 0 ? 'multiple' : i % 3 === 1 ? 'single' : 'boolean';
    specs.push({
      type,
      difficulty: 1 + (i % 3),
      ...(type === 'multiple' ? { answer: ['A', 'B'] } : {}),
    });
  }
  return specs;
}

// ---- HTTP ----
function http() {
  const agent = request(app);
  const wrap = (method, pathname, { userId, body, requestId, now } = {}) => {
    let req = agent[method](pathname);
    if (userId !== undefined) req = req.set('x-user-id', String(userId));
    if (requestId) req = req.set('x-request-id', requestId);
    if (now) req = req.set('x-now', now instanceof Date ? now.toISOString() : now);
    return req.send(body || {});
  };
  return {
    get: (p, opts) => wrap('get', p, opts),
    post: (p, opts) => wrap('post', p, opts),
  };
}

// Answers keyed by position. `score` controls how many questions are right:
// first N correct, rest wrong. Multiples are answered with one option (0
// points under exact-match unless that set happens to equal the answer).
function buildAnswers(questions, { correctCount } = {}) {
  const answers = {};
  questions.forEach((q, i) => {
    const correct = correctCount === undefined || i < correctCount;
    if (correct) {
      answers[q.position] = q.answer;
    } else if (q.type === 'boolean') {
      answers[q.position] = [q.answer[0] === 'true' ? 'false' : 'true'];
    } else {
      answers[q.position] = q.answer[0] === 'A' ? ['B'] : ['A'];
    }
  });
  return answers;
}

module.exports = {
  sequelize,
  truncateAll,
  makeOrg,
  makeUser,
  makeQuestion,
  seedQuestions,
  bankByDifficulty,
  http,
  buildAnswers,
  LETTERS,
};
