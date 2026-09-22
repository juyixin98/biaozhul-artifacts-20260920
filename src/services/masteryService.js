'use strict';

const {
  Sequelize, ExamSession, ScoreVersion, LearningActivity,
} = require('../db');
const config = require('../config');
const clock = require('../config/clock');

const DAY_MS = 86400000;
const C = config.exam;

// 掌握度 = 最近有效考试（最近一次 graded 场次的当前成绩版本）百分制成绩。
// 下降计时基准 t0 = max(最近一次学习活动时刻, 最近一次出分时刻)。
// 边界：now - t0 >= 30*24h（即满第 30 个自然日的同一时刻）起，一次性下降 5 分，最低 0。
// 不足 30 天整不降。无任何有效考试时掌握度为 null。
async function getMastery(userId, now = clock.now()) {
  const latestSession = await ExamSession.findOne({
    where: { userId, status: 'graded' },
    order: [['finalizedAt', 'DESC']],
  });
  if (!latestSession) {
    return { mastery: null, basis: null, lastActivityAt: null, decayed: false, daysSinceActivity: null };
  }
  const sv = await ScoreVersion.findByPk(latestSession.currentScoreVersionId);
  const base = Number(sv.score);

  const latestActivity = await LearningActivity.findOne({
    where: { userId },
    order: [['occurredAt', 'DESC']],
  });
  const t0Candidates = [latestSession.finalizedAt.getTime()];
  if (latestActivity) t0Candidates.push(latestActivity.occurredAt.getTime());
  const t0 = new Date(Math.max(...t0Candidates));

  const elapsedMs = now.getTime() - t0.getTime();
  const days = Math.floor(elapsedMs / DAY_MS);
  const decayed = days >= C.decayAfterDays;
  const mastery = decayed ? Math.max(0, base - C.decayPoints) : base;

  return {
    mastery,
    basis: {
      sessionId: latestSession.id,
      scoreVersionId: sv.id,
      version: sv.version,
      rawScore: base,
      finalizedAt: latestSession.finalizedAt,
    },
    lastActivityAt: t0,
    daysSinceActivity: days,
    decayBoundary: new Date(t0.getTime() + C.decayAfterDays * DAY_MS).toISOString(),
    decayed,
    rule: decayed
      ? `距最近学习已 ${days} 天（>= ${C.decayAfterDays}），掌握度 ${base} - ${C.decayPoints} = ${mastery}`
      : `距最近学习 ${days} 天（< ${C.decayAfterDays}），掌握度维持 ${base}`,
  };
}

module.exports = { getMastery };
