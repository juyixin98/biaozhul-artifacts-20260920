'use strict';

const {
  Sequelize, Certificate, ExamSession, ScoreVersion,
} = require('../db');
const config = require('../config');
const clock = require('../config/clock');
const { addUtcMonths } = require('../utils/time');
const { randomSuffix } = require('../utils/rng');
const { badRequest, conflict, notFound } = require('../errors');
const { getMastery } = require('./masteryService');

const C = config.exam;

// 发证条件（两条同时满足）：
//  1) 当前掌握度 >= 85（含 30 天无学习的下降规则）；
//  2) 当次有效考试成绩 >= 80。
// 同一场考试只发一张证书（session_id 唯一）；有效期自发证起 12 个自然月。
async function issue(user, sessionId, now = clock.now()) {
  const session = await ExamSession.findOne({ where: { id: sessionId, orgId: user.orgId } });
  if (!session) throw notFound('考试场次不存在');
  if (session.userId !== user.id) throw badRequest('只能为自己的有效考试申领证书');
  if (session.status !== 'graded') throw badRequest('考试尚未出分，无法发证');

  const sv = await ScoreVersion.findByPk(session.currentScoreVersionId);
  const score = Number(sv.score);
  const { mastery } = await getMastery(user.id, now);

  const dup = await Certificate.findOne({ where: { sessionId } });
  if (dup) {
    if (dup.status === 'revoked') throw conflict('该考试证书已被复核吊销，不可重复发证');
    throw conflict('该考试已发过证书（同一考试不重复发证）');
  }

  const reasons = [];
  if (score < C.scoreThreshold) reasons.push(`当次成绩 ${score} < ${C.scoreThreshold}`);
  if (mastery == null || mastery < C.masteryThreshold) reasons.push(`当前掌握度 ${mastery ?? '无'} < ${C.masteryThreshold}`);
  if (reasons.length) throw badRequest(`不满足发证条件：${reasons.join('；')}`);

  const issuedAt = now;
  const validUntil = addUtcMonths(issuedAt, C.certValidMonths);
  let cert;
  try {
    cert = await Certificate.create({
      certNumber: `CERT-${user.orgId}-${sessionId}-${randomSuffix(5)}`,
      orgId: user.orgId,
      userId: user.id,
      sessionId,
      score,
      masteryAtIssue: mastery,
      issuedAt,
      validUntil,
      status: 'issued',
    });
  } catch (err) {
    if (err.name === 'SequelizeUniqueConstraintError') throw conflict('该考试已发过证书（同一考试不重复发证）');
    throw err;
  }
  return toJson(cert, now);
}

// 复核后重算证书有效性：当前成绩或掌握度跌破门槛即吊销（记录原因）。
async function recalcAfterReview(session, scoreVersion, reason, transaction) {
  const cert = await Certificate.findOne({ where: { sessionId: session.id, status: 'issued' }, transaction });
  if (!cert) return 'none';
  const score = Number(scoreVersion.score);
  const now = clock.now();
  const { mastery } = await getMastery(session.userId, now);
  const failScore = score < C.scoreThreshold;
  const failMastery = mastery == null || mastery < C.masteryThreshold;
  if (failScore || failMastery) {
    cert.status = 'revoked';
    cert.revokedReason = `复核${reason ? `（${reason}）` : ''}后成绩/掌握度不再满足发证条件：score=${score}, mastery=${mastery ?? '无'}`;
    await cert.save({ transaction });
    return 'revoked';
  }
  return 'retained';
}

async function listForUser(user, now = clock.now()) {
  const certs = await Certificate.findAll({ where: { userId: user.id }, order: [['issuedAt', 'DESC']] });
  return certs.map((c) => toJson(c, now));
}

async function listOrg(actor, now = clock.now()) {
  const certs = await Certificate.findAll({ where: { orgId: actor.orgId }, order: [['issuedAt', 'DESC']] });
  return certs.map((c) => toJson(c, now));
}

// 即时有效性：status=issued 且 now < validUntil；now == validUntil 即已过期（边界明确）
function toJson(cert, now = clock.now()) {
  const expired = now.getTime() >= new Date(cert.validUntil).getTime();
  let effective = false;
  let state;
  if (cert.status === 'revoked') state = 'revoked';
  else if (expired) state = 'expired';
  else { state = 'valid'; effective = true; }
  return {
    id: cert.id,
    certNumber: cert.certNumber,
    sessionId: cert.sessionId,
    score: Number(cert.score),
    masteryAtIssue: Number(cert.masteryAtIssue),
    issuedAt: cert.issuedAt,
    validUntil: cert.validUntil,
    status: cert.status,
    effectiveState: state,
    effective,
    revokedReason: cert.revokedReason,
  };
}

module.exports = { issue, recalcAfterReview, listForUser, listOrg, toJson };
