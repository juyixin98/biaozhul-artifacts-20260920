'use strict';

const {
  sequelize, Sequelize, ExamSession, Submission, PaperQuestion, Paper,
  ScoreVersion, LearningActivity, User,
} = require('../db');
const config = require('../config');
const clock = require('../config/clock');
const { dayBoundsInZone } = require('../utils/time');
const { grade, hashAnswers } = require('./gradingService');
const { badRequest, forbidden, notFound, conflict, locked } = require('../errors');

const C = config.exam;
const ms = (m) => m * 60000;

// 在同一连接上申请命名锁（必须与事务内查询共连接，才能串行化并发请求）
async function acquireLock(t, name, timeoutSeconds = 10) {
  const [rows] = await t.sequelize.query('SELECT GET_LOCK(:name, :timeout) AS got', {
    transaction: t, replacements: { name, timeout: timeoutSeconds },
  });
  if (!rows[0].got) throw locked('系统繁忙，请稍后重试（并发锁获取失败）');
}
async function releaseLock(t, name) {
  await t.sequelize.query('SELECT RELEASE_LOCK(:name)', { transaction: t, replacements: { name } });
}

// 开始考试：组织时区自然日 00:00–24:00 内最多 3 次；并发开考在命名锁内计数，不会突破上限
async function startExam(user, paperId) {
  const paper = await Paper.findOne({ where: { id: paperId, orgId: user.orgId } });
  if (!paper) throw notFound('试卷不存在');

  const now = clock.now();
  const lockName = `start:u${user.id}`;

  return sequelize.transaction(async (t) => {
    await acquireLock(t, lockName);
    try {
      // 命名锁在提交前即释放，因此再对用户行加 X 锁（持到提交）：
      // 后到事务在此阻塞到本事务提交，确保 COUNT 能看到已提交的开考记录，杜绝突破上限的窗口。
      await User.findByPk(user.id, { transaction: t, lock: t.LOCK.UPDATE });
      const { start, end } = dayBoundsInZone(now, user.organization.timezone);
      const count = await ExamSession.count({
        where: { userId: user.id, startedAt: { [Sequelize.Op.gte]: start, [Sequelize.Op.lt]: end } },
        transaction: t,
      });
      if (count >= C.dailyStartLimit) {
        throw forbidden(`今日（组织时区 ${user.organization.timezone}）开考已达上限 ${C.dailyStartLimit} 次，边界 [${start.toISOString()}, ${end.toISOString()})`);
      }

      const deadline = new Date(now.getTime() + ms(C.durationMinutes));
      const session = await ExamSession.create({
        orgId: user.orgId,
        paperId,
        userId: user.id,
        status: 'in_progress',
        startedAt: now,
        deadlineAt: deadline,
      }, { transaction: t });

      return {
        sessionId: session.id,
        paperId,
        startedAt: now,
        deadlineAt: deadline,
        durationMinutes: C.durationMinutes,
        dailyUsed: count + 1,
        dailyLimit: C.dailyStartLimit,
        dayWindow: { start: start.toISOString(), end: end.toISOString(), timezone: user.organization.timezone },
      };
    } finally {
      await releaseLock(t, lockName);
    }
  });
}

// 锁定场次并读取快照；终态/归属/存在性校验集中于此
async function loadSessionForWrite(t, sessionId, user) {
  await acquireLock(t, `session:s${sessionId}`, 10);
  const session = await ExamSession.findByPk(sessionId, {
    include: [{ model: Paper, as: 'paper' }],
    transaction: t,
    lock: t.LOCK.UPDATE,
  });
  if (!session) throw notFound('考试场次不存在');
  if (session.orgId !== user.orgId) throw notFound('考试场次不存在');
  if (user.role === 'student' && session.userId !== user.id) throw forbidden('学员只能访问自己的考试');
  return session;
}

// 提交考试（幂等 + 冲突检测 + 终态唯一）
async function submit(user, sessionId, body) {
  const requestId = body.requestId;
  if (!requestId) throw badRequest('requestId 必填');
  if (requestId.length > 64) throw badRequest('requestId 过长');
  if (!body.answers || typeof body.answers !== 'object') throw badRequest('answers 必填且为对象');

  const answersHash = hashAnswers(body.answers);
  const now = clock.now();

  return sequelize.transaction(async (t) => {
    // 1) 幂等/冲突以全局唯一 requestId 为准（与场次锁无死锁：先查 requestId 行）
    const priorByRequest = await Submission.findOne({ where: { requestId }, transaction: t });
    if (priorByRequest) {
      if (priorByRequest.sessionId !== Number(sessionId)) {
        throw conflict('requestId 已被其他考试使用');
      }
      if (priorByRequest.answersHash !== answersHash) {
        throw conflict('相同 requestId 提交了不同内容（409 Conflict）');
      }
      // 同 ID 同内容：无论场次是否已终态，返回原成绩
      const sv = await ScoreVersion.findOne({ where: { sessionId: priorByRequest.sessionId, isCurrent: true }, transaction: t });
      return { idempotent: true, result: sv ? serializeScore(sv) : null };
    }

    const session = await loadSessionForWrite(t, sessionId, user);
    if (session.status === 'graded') {
      // 场次已有终态但用了新 requestId：拒绝，终态唯一（原成绩需用原 requestId 重放）
      throw conflict('该考试已结束并评分，不能再次交卷');
    }

    // 2) 时间边界：t < deadlineAt 为按时；t >= deadlineAt 即超时（边界明确，含 deadline 整时刻）
    const late = now.getTime() >= session.deadlineAt.getTime();
    let submission;
    try {
      submission = await Submission.create({
        sessionId,
        requestId,
        answersHash,
        answers: body.answers,
        clientSubmittedAt: body.clientSubmittedAt || null,
        arrival: late ? 'late' : 'arrived',
      }, { transaction: t });
    } catch (err) {
      if (err.name === 'SequelizeUniqueConstraintError') {
        // 并发下另一请求已占用 requestId：同内容回放原成绩，不同内容冲突
        const winner = await Submission.findOne({ where: { requestId }, transaction: t });
        if (winner && winner.answersHash === answersHash) {
          const sv = await ScoreVersion.findOne({ where: { sessionId, isCurrent: true }, transaction: t });
          return { idempotent: true, result: sv ? serializeScore(sv) : null };
        }
        throw conflict('相同 requestId 提交了不同内容（409 Conflict）');
      }
      throw err;
    }

    // 3) 评分（使用冻结快照）并落唯一终态
    const pqs = await PaperQuestion.findAll({ where: { paperId: session.paperId }, order: [['order', 'ASC']], transaction: t });
    const { score, detail } = grade(pqs, body.answers);
    const finalizedAt = now;
    const version = await createScoreVersion(t, {
      session, score, detail, source: 'submit', reason: late ? '超时自动交卷' : null, reviewerId: null,
    });

    session.status = 'graded';
    session.submittedAt = late ? session.deadlineAt : now; // 超时提交的提交时刻记为截止时刻
    session.finalizedAt = finalizedAt;
    session.finalReason = late ? 'timeout' : 'submit';
    session.currentScoreVersionId = version.id;
    await session.save({ transaction: t });

    // 有效考试即一次学习活动，重置掌握度下降计时
    await LearningActivity.create({
      orgId: session.orgId, userId: session.userId, type: 'exam', refId: session.id, occurredAt: finalizedAt,
    }, { transaction: t });

    return {
      idempotent: false,
      result: serializeScore(version, { late, deadlineAt: session.deadlineAt, finalizedAt, submissionId: submission.id }),
    };
  });
}

// 超时收尾：把所有已过 deadline 仍 in_progress 的场次按“当前无作答”判 0 分终态。
// 与交卷并发安全：两者抢同一把 session 命名锁 + 行锁，先到者置 graded，后者只看到终态。
async function finalizeExpired(now = clock.now(), { orgId } = {}) {
  // 不加外层锁：逐场次开事务、锁行；与交卷共享 session 命名锁，终态唯一
  const expired = await ExamSession.findAll({
    where: { status: 'in_progress', deadlineAt: { [Sequelize.Op.lte]: now }, ...(orgId ? { orgId } : {}) },
  });
  const results = [];
  for (const s of expired) {
    // eslint-disable-next-line no-await-in-loop
    const out = await sequelize.transaction(async (t) => {
      const session = await ExamSession.findByPk(s.id, { transaction: t, lock: t.LOCK.UPDATE });
      if (!session || session.status === 'graded' || session.deadlineAt.getTime() > now.getTime()) return null;
      const pqs = await PaperQuestion.findAll({ where: { paperId: session.paperId }, order: [['order', 'ASC']], transaction: t });
      const { score, detail } = grade(pqs, {}); // 超时未交：无答案，0 分
      const version = await createScoreVersion(t, {
        session, score, detail, source: 'submit', reason: '超时未交卷，系统自动判 0 分', reviewerId: null,
      });
      session.status = 'graded';
      session.submittedAt = session.deadlineAt;
      session.finalizedAt = now;
      session.finalReason = 'timeout';
      session.currentScoreVersionId = version.id;
      await session.save({ transaction: t });
      await LearningActivity.create({
        orgId: session.orgId, userId: session.userId, type: 'exam', refId: session.id, occurredAt: now,
      }, { transaction: t });
      return { sessionId: session.id, score };
    });
    if (out) results.push(out);
  }
  return results;
}

// 创建成绩版本：先把旧版本 isCurrent 置否，再插入新版本（append-only，历史行不改分数）
async function createScoreVersion(t, { session, score, detail, source, reason, reviewerId }) {
  if (source === 'review' && !reason) throw badRequest('复核必须填写原因');
  const last = await ScoreVersion.findOne({ where: { sessionId: session.id }, order: [['version', 'DESC']], transaction: t });
  const nextVersion = last ? last.version + 1 : 1;
  await ScoreVersion.update({ isCurrent: false }, { where: { sessionId: session.id }, transaction: t });
  return ScoreVersion.create({
    sessionId: session.id, version: nextVersion, score, detail, source, reason, reviewerId, isCurrent: true,
  }, { transaction: t });
}

// 学员成绩视图：出分后返回判分明细（含错题冻结版本）；未出分不返回任何答案
async function getResult(user, sessionId) {
  const session = await ExamSession.findOne({ where: { id: sessionId, orgId: user.orgId } });
  if (!session) throw notFound('考试场次不存在');
  if (user.role === 'student' && session.userId !== user.id) throw forbidden('学员只能查看自己的考试');

  if (session.status !== 'graded') {
    return { sessionId, status: 'in_progress', startedAt: session.startedAt, deadlineAt: session.deadlineAt };
  }
  const sv = await ScoreVersion.findByPk(session.currentScoreVersionId);
  return {
    sessionId,
    status: 'graded',
    finalReason: session.finalReason,
    startedAt: session.startedAt,
    deadlineAt: session.deadlineAt,
    finalizedAt: session.finalizedAt,
    score: serializeScore(sv),
    // 错题（冻结版本），供学员复盘
    wrongQuestions: sv.detail.filter((d) => !d.correct).map((d) => ({
      order: d.order, questionId: d.questionId, type: d.type, stem: d.stem, options: d.options,
      given: d.given, explanation: d.explanation, points: d.points,
    })),
  };
}

function serializeScore(sv, extra = {}) {
  return {
    scoreVersionId: sv.id,
    version: sv.version,
    score: Number(sv.score),
    source: sv.source,
    reason: sv.reason,
    ...extra,
  };
}

module.exports = {
  startExam, submit, finalizeExpired, getResult, createScoreVersion,
};
