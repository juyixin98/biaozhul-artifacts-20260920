'use strict';

const crypto = require('crypto');

// 规范化答案：多选题选项排序后比较；判断题归一布尔；其余按字符串
function normalizeAnswer(v) {
  if (Array.isArray(v)) return [...v].map(String).sort();
  if (typeof v === 'boolean') return v;
  return v == null ? null : String(v);
}

// 规范化答案哈希：按题目序号排序后序列化，避免键顺序差异
function hashAnswers(answers) {
  const normalized = {};
  Object.keys(answers || {}).sort((a, b) => Number(a) - Number(b)).forEach((k) => {
    normalized[k] = normalizeAnswer(answers[k]);
  });
  return crypto.createHash('sha256').update(JSON.stringify(normalized)).digest('hex');
}

module.exports = { hashAnswers, normalizeAnswer };
