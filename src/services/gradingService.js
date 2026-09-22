'use strict';

const { hashAnswers } = require('../utils/answers');

// 比较学员答案与冻结标准答案。
// single / judge：严格相等；multiple：选项集合完全一致（排序后比较），少选、多选均 0 分。
function isCorrect(type, given, correct) {
  if (given === undefined || given === null) return false;
  if (type === 'multiple') {
    if (!Array.isArray(given)) return false;
    const g = [...given].map(String).sort();
    const c = Array.isArray(correct) ? [...correct].map(String).sort() : [String(correct)];
    return g.length === c.length && g.every((v, i) => v === c[i]);
  }
  if (type === 'judge') return Boolean(given) === Boolean(correct);
  return String(given) === String(correct);
}

// 依据冻结的试卷题目快照评分，产出：百分制成绩 + 每题明细（错题版本即冻结快照）
function grade(paperQuestions, answers) {
  let earned = 0;
  let totalPoints = 0;
  const detail = paperQuestions.map((pq) => {
    const points = Number(pq.points);
    totalPoints += points;
    const given = answers ? answers[String(pq.order)] : undefined;
    const correct = isCorrect(pq.type, given, pq.answer);
    if (correct) earned += points;
    return {
      order: pq.order,
      questionId: pq.questionId,
      type: pq.type,
      difficulty: pq.difficulty,
      stem: pq.stem, // 冻结版本
      options: pq.options,
      explanation: pq.explanation,
      points,
      given: given === undefined ? null : given,
      correct,
      awarded: correct ? points : 0,
    };
  });
  const score = totalPoints === 0 ? 0 : Math.round((earned / totalPoints) * 10000) / 100; // 两位小数
  return { score, earned, totalPoints, detail };
}

module.exports = { grade, isCorrect, hashAnswers };
