'use strict';

const { sequelize, Question, Paper, PaperQuestion } = require('../db');
const { createRng, shuffled } = require('../utils/rng');
const config = require('../config');
const clock = require('../config/clock');
const { badRequest, notFound } = require('../errors');

const C = config.exam;

// 按种子确定性生成试卷。
// 规则：20 题；难度 >=4 的题至少 6 道（20*30% 向上取整）；题库不足时明确失败，
// 绝不以重复题或降标准凑数。题目（题干/选项/答案/解析/分值）在生成时整体冻结。
async function generatePaper(orgId, seed, title) {
  if (!seed) throw badRequest('seed 必填');

  // 幂等：同组织同种子直接返回已冻结的原试卷
  const existing = await Paper.findOne({ where: { orgId, seed }, include: [{ model: PaperQuestion, as: 'questions' }] });
  if (existing) return existing;

  const pool = await Question.findAll({ where: { orgId, active: true } });
  const total = C.questionCount;
  const hardNeed = Math.ceil(total * C.hardMinRatio); // 6
  const hardPool = pool.filter((q) => q.difficulty >= C.hardMinDifficulty);
  const otherPool = pool.filter((q) => q.difficulty < C.hardMinDifficulty);

  // 题目不足——明确报告缺什么、缺多少，不重复凑数
  if (pool.length < total) {
    throw badRequest(`题库题目不足：需要 ${total} 题，当前仅 ${pool.length} 题（且不允许重复抽题）`);
  }
  if (hardPool.length < hardNeed) {
    throw badRequest(
      `高难度题目不足：难度 >= ${C.hardMinDifficulty} 的题至少需要 ${hardNeed} 道，当前仅 ${hardPool.length} 道`
    );
  }

  const rng = createRng(`${orgId}:${seed}`);
  const hardPicked = shuffled(hardPool, rng).slice(0, hardNeed);
  const hardIds = new Set(hardPicked.map((q) => q.id));
  const restPool = otherPool.concat(hardPool.filter((q) => !hardIds.has(q.id)));
  const restPicked = shuffled(restPool, rng).slice(0, total - hardNeed);
  const picked = shuffled(hardPicked.concat(restPicked), rng); // 题目顺序也由种子决定

  const totalPoints = picked.reduce((s, q) => s + Number(q.points), 0);

  return sequelize.transaction(async (t) => {
    // 唯一索引兜底并发：两个相同种子请求只有一个能插入
    let paper;
    try {
      paper = await Paper.create({
        orgId,
        seed,
        title: title || `能力考核卷 ${seed}`,
        questionCount: total,
        scoringRule: {
          perQuestion: '每题独立判分；单选/判断须与标准答案一致；多选须选项集合完全一致，少选/多选均 0 分',
          totalPoints,
          scale: '得分换算为百分制 round(100*earned/totalPoints)',
        },
      }, { transaction: t });
    } catch (err) {
      if (err.name === 'SequelizeUniqueConstraintError') {
        return Paper.findOne({ where: { orgId, seed }, include: [{ model: PaperQuestion, as: 'questions' }], transaction: t });
      }
      throw err;
    }

    await PaperQuestion.bulkCreate(picked.map((q, i) => ({
      paperId: paper.id,
      questionId: q.id,
      order: i + 1,
      type: q.type,
      difficulty: q.difficulty,
      stem: q.stem,
      options: q.options,
      answer: q.answer, // 答案一并冻结；对学员接口由序列化层隔离
      explanation: q.explanation,
      points: q.points,
    })), { transaction: t });

    return Paper.findByPk(paper.id, { include: [{ model: PaperQuestion, as: 'questions' }], transaction: t });
  });
}

async function getPaper(orgId, id) {
  const paper = await Paper.findOne({
    where: { id, orgId },
    include: [{ model: PaperQuestion, as: 'questions', separate: true, order: [['order', 'ASC']] }],
  });
  if (!paper) throw notFound('试卷不存在');
  return paper;
}

// 学员视图：不含 answer / explanation（提交前不泄露答案）
function toStudentJson(paper) {
  return {
    id: paper.id,
    seed: paper.seed,
    title: paper.title,
    questionCount: paper.questionCount,
    scoringRule: { totalPoints: paper.scoringRule.totalPoints, scale: paper.scoringRule.scale },
    questions: paper.questions.map((q) => ({
      order: q.order,
      type: q.type,
      difficulty: q.difficulty,
      stem: q.stem,
      options: q.options,
      points: Number(q.points),
    })),
    generatedAt: paper.createdAt,
    serverTime: clock.now(),
  };
}

// 管理视图：含冻结答案与解析
function toAdminJson(paper) {
  return {
    ...toStudentJson(paper),
    scoringRule: paper.scoringRule,
    questions: paper.questions.map((q) => ({
      order: q.order,
      questionId: q.questionId,
      type: q.type,
      difficulty: q.difficulty,
      stem: q.stem,
      options: q.options,
      answer: q.answer,
      explanation: q.explanation,
      points: Number(q.points),
    })),
  };
}

module.exports = { generatePaper, getPaper, toStudentJson, toAdminJson };
