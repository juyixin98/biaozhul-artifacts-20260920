'use strict';

const {
  sequelize, ExamSession, ScoreVersion, Certificate,
} = require('../db');
const { badRequest, notFound } = require('../errors');
const { createScoreVersion } = require('./examService');
const certificateService = require('./certificateService');

// 复核：不修改历史成绩，只新增一个版本并记录原因。
// body.adjustments: { "<order>": awardedPoints } —— 主管对每题给定裁定得分。
// 复核后重算百分制；成绩下降时同步重算证书有效性（失效则吊销）。
async function reviewSession(actor, sessionId, body) {
  if (!body || !body.reason) throw badRequest('复核原因 reason 必填');
  const adjustments = body.adjustments || {};

  return sequelize.transaction(async (t) => {
    const session = await ExamSession.findByPk(sessionId, { transaction: t, lock: t.LOCK.UPDATE });
    if (!session || session.orgId !== actor.orgId) throw notFound('考试场次不存在');
    if (session.status !== 'graded') throw badRequest('仅已出分考试可复核');

    const current = await ScoreVersion.findByPk(session.currentScoreVersionId, { transaction: t });
    const detail = current.detail.map((d) => {
      const key = String(d.order);
      if (!(key in adjustments)) return { ...d };
      const awarded = Number(adjustments[key]);
      if (Number.isNaN(awarded) || awarded < 0 || awarded > d.points) {
        throw badRequest(`第 ${d.order} 题裁定得分须在 0–${d.points} 之间`);
      }
      return {
        ...d,
        awarded,
        correct: awarded >= d.points,
        reviewed: true,
      };
    });

    const totalPoints = detail.reduce((s, d) => s + d.points, 0);
    const earned = detail.reduce((s, d) => s + Number(d.awarded), 0);
    const score = Math.round((earned / totalPoints) * 10000) / 100;

    const version = await createScoreVersion(t, {
      session, score, detail, source: 'review', reason: body.reason, reviewerId: actor.id,
    });
    session.currentScoreVersionId = version.id;
    await session.save({ transaction: t });

    // 复核（尤其降分）后联动重算证书
    const cert = await Certificate.findOne({ where: { sessionId }, transaction: t });
    let certificateAction = 'none';
    if (cert) {
      const recalc = await certificateService.recalcAfterReview(session, version, body.reason, t);
      certificateAction = recalc;
    }

    return {
      sessionId,
      previous: { version: current.version, score: Number(current.score) },
      current: { version: version.version, score: Number(version.score), reason: version.reason },
      lowered: score < Number(current.score),
      certificateAction,
    };
  });
}

module.exports = { reviewSession };
