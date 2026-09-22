'use strict';

const request = require('supertest');
const { createApp } = require('../src/app');
const { Organization, User, Question } = require('../src/db');

const app = createApp();

async function createOrg(id = 1, timezone = 'UTC') {
  return Organization.create({ id, name: `Org ${id}`, timezone });
}

async function createUser({ id, orgId = 1, role = 'student', name, token }) {
  return User.create({
    id, orgId, role, name: name || `u${id}`, apiToken: token || `token-${id}`,
  });
}

const auth = (token) => ({ Authorization: `Bearer ${token}` });

// 批量造题：按给定难度/题型分布
async function seedQuestions(orgId, specs) {
  return Question.bulkCreate(specs.map((s, i) => ({
    orgId,
    type: s.type || 'single',
    difficulty: s.difficulty,
    stem: s.stem || `Q${i + 1} d${s.difficulty} ${s.type || 'single'}`,
    options: (s.type || 'single') === 'judge' ? null : (s.options || [
      { key: 'A', text: 'a' }, { key: 'B', text: 'b' }, { key: 'C', text: 'c' }, { key: 'D', text: 'd' },
    ]),
    answer: s.answer !== undefined ? s.answer : ((s.type || 'single') === 'judge' ? true : 'A'),
    explanation: s.explanation || '解析',
    points: s.points || 5,
  })));
}

// 每种难度 1–5 各 n 题（混合题型），默认共 5n 题
async function seedBalancedQuestions(orgId, perDifficulty = 8) {
  const specs = [];
  for (let d = 1; d <= 5; d += 1) {
    for (let i = 0; i < perDifficulty; i += 1) {
      const type = i % 3 === 0 ? 'multiple' : i % 3 === 1 ? 'single' : 'judge';
      specs.push({ difficulty: d, type, answer: type === 'multiple' ? ['A', 'B'] : type === 'judge' ? true : 'A' });
    }
  }
  return seedQuestions(orgId, specs);
}

function answersFor(paperJson, fn) {
  const answers = {};
  paperJson.questions.forEach((q) => {
    answers[q.order] = fn(q);
  });
  return answers;
}

module.exports = {
  app, request, createOrg, createUser, auth, seedQuestions, seedBalancedQuestions, answersFor,
};
