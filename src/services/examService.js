const {
  sequelize,
  Exam,
  Paper,
  PaperQuestion,
  DailyAttemptCount,
  Submission,
  ScoreVersion,
  WrongQuestion,
  User,
  Organization,
} = require('../models');
const { Sequelize, Op } = require('sequelize');
const config = require('../config');
const {
  addMinutes,
  dayKeyInTimezone,
  mulberry32,
} = require('../utils/time');
const { gradePaper, hashAnswers } = require('./grading');
const { computeMastery } = require('./masteryService');
const { issueForExamIfEligible, revalidateForExam, serialize: serializeCert } = require('./certificateService');
const { conflict, badRequest, notFound, forbidden, unprocessable } = require('../utils/errors');

const TERMINAL_STATUSES = ['submitted', 'expired'];

// ---------- Starting an attempt ----------
//
// Concurrency guarantee: two simultaneous "start" requests for the same
// user cannot both consume the 3rd daily slot. Inside a SERIALIZABLE
// transaction we lock the user row (stable lock target), read/create the
// day counter, check the limit, then increment and create the exam.
// SERIALIZABLE additionally guarantees the counter's unique key insert
// races resolve with one winner.
async function startExam({ user, paperId, now }) {
  const paper = await Paper.findByPk(paperId);
  if (!paper || paper.status !== 'active') throw notFound('active paper not found');

  const org = user.organizationId ? await Organization.findByPk(user.organizationId) : null;
  const tz = (org && org.timezone) || 'UTC';
  const dayKey = dayKeyInTimezone(now, tz);

  return sequelize.transaction(
    { isolationLevel: Sequelize.Transaction.ISOLATION_LEVELS.SERIALIZABLE },
    async (t) => {
      // Lock a stable row so concurrent starts for one user serialize.
      await User.findByPk(user.id, { transaction: t, lock: t.LOCK.UPDATE });

      const [counter] = await DailyAttemptCount.findOrCreate({
        where: { userId: user.id, dayKey },
        defaults: { userId: user.id, dayKey, count: 0 },
        transaction: t,
      });
      await counter.reload({ transaction: t, lock: t.LOCK.UPDATE });

      if (counter.count >= config.DAILY_ATTEMPT_LIMIT) {
        throw conflict(
          `daily attempt limit reached: ${config.DAILY_ATTEMPT_LIMIT} starts per ${tz} day (${dayKey}); try again after local midnight`,
          'DAILY_LIMIT_REACHED'
        );
      }

      const seedStr = `u${user.id}-p${paperId}-${dayKey}-n${counter.count}-${now.getTime()}`;
      const exam = await Exam.create(
        {
          paperId,
          userId: user.id,
          startedAt: now,
          deadlineAt: addMinutes(now, paper.durationMinutes),
          presentationSeed: mulberry32(seedStr)().toString(16).slice(2, 10),
          status: 'in_progress',
        },
        { transaction: t }
      );
      counter.count += 1;
      await counter.save({ transaction: t });

      return loadTakingView(exam, paper, t);
    }
  );
}

// Student-facing exam view: questions WITHOUT answer/explanation/scoring
// internals — answers must never leak before (or during) the exam.
async function loadTakingView(exam, paperArg, transaction) {
  const paper = paperArg || (await Paper.findByPk(exam.paperId, { transaction }));
  const questions = await PaperQuestion.findAll({
    where: { paperId: paper.id },
    order: [['position', 'ASC']],
    transaction,
  });
  return {
    id: exam.id,
    paperId: paper.id,
    title: paper.title,
    status: exam.status,
    startedAt: exam.startedAt.toISOString(),
    deadlineAt: exam.deadlineAt.toISOString(),
    durationMinutes: paper.durationMinutes,
    questions: questions.map((q) => ({
      paperQuestionId: q.id,
      position: q.position,
      type: q.type,
      difficulty: q.difficulty,
      stem: q.stem,
      options: q.options,
    })),
  };
}

async function getTakingView(examId, user) {
  const exam = await Exam.findByPk(examId);
  if (!exam) throw notFound('exam not found');
  if (exam.userId !== user.id && user.role !== 'admin') {
    throw forbidden('students may only view their own exams');
  }
  return loadTakingView(exam);
}

// ---------- Submission & idempotent grading ----------
async function submitExam({ user, examId, requestId, answers, now }) {
  if (!requestId || typeof requestId !== 'string' || requestId.length > 128) {
    throw badRequest('requestId is required (max 128 chars) and is the idempotency key');
  }
  if (answers === undefined || answers === null) throw badRequest('answers payload is required');

  return sequelize.transaction(async (t) => {
    // Lock the exam row first: any concurrent submit/expire blocks here,
    // so exactly one path produces the terminal state.
    const exam = await Exam.findByPk(examId, { transaction: t, lock: t.LOCK.UPDATE });
    if (!exam) throw notFound('exam not found');
    if (exam.userId !== user.id) throw forbidden('students may only submit their own exams');

    // Idempotency check for THIS request id.
    const prior = await Submission.findOne({ where: { examId, requestId }, transaction: t });
    if (prior) {
      const sameContent = prior.contentHash === hashAnswers(answers);
      if (!sameContent) {
        throw conflict(
          `requestId '${requestId}' was already used for this exam with different content`,
          'IDEMPOTENCY_CONFLICT'
        );
      }
      // Same id + same content: replay returns the ORIGINAL result untouched.
      const version = await ScoreVersion.findByPk(prior.scoreVersionId, { transaction: t });
      const cert = await CertificateSafeFind(examId, t, now);
      return { replay: true, result: serializeResult(exam, version, cert, now) };
    }

    // A different request id arriving after the terminal state already
    // exists cannot create a second final result.
    if (TERMINAL_STATUSES.includes(exam.status)) {
      const existing = await Submission.findOne({
        where: { examId },
        order: [['id', 'ASC']],
        transaction: t,
      });
      const version = exam.finalScoreVersionId
        ? await ScoreVersion.findByPk(exam.finalScoreVersionId, { transaction: t })
        : null;
      if (existing && version) {
        return { replay: false, alreadyTerminal: true, result: serializeResult(exam, version, null, now) };
      }
      // Terminal via expire-before-submit: record this submission but score 0.
      return finalizeZero({ exam, t, requestId, answers, now, status: exam.status });
    }

    const paperQuestions = await PaperQuestion.findAll({
      where: { paperId: exam.paperId },
      order: [['position', 'ASC']],
      transaction: t,
      lock: t.LOCK.UPDATE,
    });

    const overdue = now.getTime() > exam.deadlineAt.getTime();
    const result = overdue
      ? await finalizeZero({ exam, t, requestId, answers, now, status: 'expired' })
      : await finalizeGraded({ exam, t, paperQuestions, requestId, answers, now });

    return result;
  });
}

async function CertificateSafeFind(examId, t, now) {
  const { Certificate } = require('../models');
  const cert = await Certificate.findOne({ where: { examId }, transaction: t });
  return cert ? serializeCert(cert, now) : null;
}

function persistScoreArtifacts({ exam, t, paperQuestions, grading, requestId, answers, submittedAt, status }) {
  return (async () => {
    const version = await ScoreVersion.create(
      {
        examId: exam.id,
        version: 1,
        score: grading.score,
        correctCount: grading.correctCount,
        wrongCount: grading.wrongCount,
        gradingDetail: grading.detail,
        source: 'auto',
        createdAt: submittedAt,
      },
      { transaction: t }
    );

    await Submission.create(
      {
        examId: exam.id,
        requestId,
        contentHash: hashAnswers(answers),
        answers,
        scoreVersionId: version.id,
        createdAt: submittedAt,
      },
      { transaction: t }
    );

    const wrongRows = grading.detail
      .filter((d) => !d.correct)
      .map((d) => ({
        examId: exam.id,
        userId: exam.userId,
        paperQuestionId: d.paperQuestionId,
        givenAnswer: d.givenAnswer,
        correctAnswer: d.correctAnswer,
        scoreVersion: 1,
      }));
    if (wrongRows.length) await WrongQuestion.bulkCreate(wrongRows, { transaction: t });

    exam.status = status;
    exam.submittedAt = submittedAt;
    exam.finalScoreVersionId = version.id;
    await exam.save({ transaction: t });

    return version;
  })();
}

async function finalizeGraded({ exam, t, paperQuestions, requestId, answers, now }) {
  const grading = gradePaper(paperQuestions, answers);
  const version = await persistScoreArtifacts({
    exam,
    t,
    paperQuestions,
    grading,
    requestId,
    answers,
    submittedAt: now,
    status: 'submitted',
  });

  // Mastery for the issue gate is computed inside the same transaction,
  // based on the just-written latest valid score.
  const mastery = await computeMastery(exam.userId, now, { transaction: t });
  const cert = await issueForExamIfEligible(
    exam,
    { score: grading.score, mastery: mastery.mastery, now, transaction: t }
  );

  return {
    replay: false,
    timedOut: false,
    result: serializeResult(exam, version, cert ? serializeCert(cert, now) : null, now),
  };
}

// Late submission or expire-sweep terminalization: score 0, all questions
// wrong. The given answers are still recorded for audit, and a submission
// row with the client request id keeps replay idempotency.
async function finalizeZero({ exam, t, requestId, answers, now, status }) {
  const paperQuestions = await PaperQuestion.findAll({
    where: { paperId: exam.paperId },
    order: [['position', 'ASC']],
    transaction: t,
  });
  const grading = gradePaper(paperQuestions, {}); // no answer set scores 0
  const version = await persistScoreArtifacts({
    exam,
    t,
    paperQuestions,
    grading,
    requestId: requestId || `timeout-${exam.id}`,
    answers: answers || {},
    submittedAt: now,
    status,
  });
  return {
    replay: false,
    timedOut: true,
    result: serializeResult(exam, version, null, now),
  };
}

// ---------- Timeout ----------
// Idempotent terminalization of an in-progress exam whose deadline passed.
// Concurrent timeout calls collapse onto the same terminal state.
async function expireIfDue(examId, now, options = {}) {
  return sequelize.transaction(async (t) => {
    const exam = await Exam.findByPk(examId, { transaction: t, lock: t.LOCK.UPDATE });
    if (!exam) throw notFound('exam not found');
    if (options.user && exam.userId !== options.user.id && options.user.role !== 'admin') {
      throw forbidden('students may only access their own exams');
    }

    if (TERMINAL_STATUSES.includes(exam.status)) {
      const version = await ScoreVersion.findByPk(exam.finalScoreVersionId, { transaction: t });
      return { changed: false, result: version ? serializeResult(exam, version, null, now) : null };
    }
    if (now.getTime() < exam.deadlineAt.getTime() && !options.force) {
      return { changed: false, result: null, deadlineAt: exam.deadlineAt.toISOString() };
    }
    const out = await finalizeZero({ exam, t, requestId: null, answers: {}, now, status: 'expired' });
    return { changed: true, result: out.result };
  });
}

async function sweepExpired(now) {
  const due = await Exam.findAll({
    where: { status: 'in_progress', deadlineAt: { [Op.lte]: now } },
    attributes: ['id'],
  });
  let expired = 0;
  for (const row of due) {
    const r = await expireIfDue(row.id, now);
    if (r.changed) expired += 1;
  }
  return { scanned: due.length, expired };
}

// ---------- Review (复核): append-only new score version ----------
async function reviewExam({ reviewer, examId, score, reason, now }) {
  if (!Number.isInteger(score) || score < 0 || score > 100) {
    throw badRequest('score must be an integer 0..100');
  }
  if (!reason || !String(reason).trim()) {
    throw badRequest('reason is mandatory for a review (audit trail)');
  }

  return sequelize.transaction(async (t) => {
    const exam = await Exam.findByPk(examId, { transaction: t, lock: t.LOCK.UPDATE });
    if (!exam) throw notFound('exam not found');
    if (TERMINAL_STATUSES.includes(exam.status) === false) {
      throw unprocessable('cannot review an exam that has no final score yet');
    }
    const student = await User.findByPk(exam.userId, { transaction: t });
    if (
      reviewer.role !== 'admin' &&
      !(reviewer.role === 'supervisor' && student.organizationId === reviewer.organizationId)
    ) {
      throw forbidden('supervisors may only review exams within their own organization');
    }

    const lastVersion = await ScoreVersion.findOne({
      where: { examId },
      order: [['version', 'DESC']],
      transaction: t,
      lock: t.LOCK.UPDATE,
    });

    // Carry the frozen per-question detail; the review sets the aggregate
    // score. Historical rows are untouched — the new row is version n+1.
    const newVersion = await ScoreVersion.create(
      {
        examId,
        version: lastVersion.version + 1,
        score,
        correctCount: lastVersion.correctCount,
        wrongCount: lastVersion.wrongCount,
        gradingDetail: lastVersion.gradingDetail,
        source: 'review',
        reason: String(reason).slice(0, 500),
        reviewerId: reviewer.id,
        createdAt: now,
      },
      { transaction: t }
    );
    exam.finalScoreVersionId = newVersion.id;
    await exam.save({ transaction: t });

    // Lowered score -> re-evaluate the certificate issued from this exam.
    const cert = await revalidateForExam(examId, score, String(reason), { now, transaction: t });
    return {
      version: serializeVersion(newVersion, exam),
      certificate: cert ? serializeCert(cert, now) : null,
    };
  });
}

// ---------- Reads ----------
function serializeVersion(version, exam) {
  return {
    examId: exam.id,
    version: version.version,
    score: version.score,
    correctCount: version.correctCount,
    wrongCount: version.wrongCount,
    source: version.source,
    reason: version.reason,
    reviewerId: version.reviewerId,
    createdAt: version.createdAt.toISOString(),
  };
}

function serializeResult(exam, version, cert, now) {
  return {
    examId: exam.id,
    paperId: exam.paperId,
    status: exam.status,
    startedAt: exam.startedAt.toISOString(),
    deadlineAt: exam.deadlineAt.toISOString(),
    submittedAt: exam.submittedAt ? exam.submittedAt.toISOString() : null,
    timedOut: exam.status === 'expired',
    score: version.score,
    correctCount: version.correctCount,
    wrongCount: version.wrongCount,
    // Answers/explanations are exposed only here, in the graded result.
    detail: version.gradingDetail.map((d) => ({
      position: d.position,
      type: d.type,
      givenAnswer: d.givenAnswer,
      correctAnswer: d.correctAnswer,
      correct: d.correct,
      pointsEarned: d.pointsEarned,
      points: d.points,
      explanation: d.explanation,
    })),
    certificate: cert,
    evaluatedAt: now.toISOString(),
  };
}

async function getResult(examId, user, now) {
  const exam = await Exam.findByPk(examId);
  if (!exam) throw notFound('exam not found');
  if (
    exam.userId !== user.id &&
    !(user.role === 'admin') &&
    !(user.role === 'supervisor' && (await sameOrg(user, exam.userId)))
  ) {
    throw forbidden('students may only view their own exams');
  }
  if (!TERMINAL_STATUSES.includes(exam.status)) {
    return { examId, status: exam.status, message: 'exam in progress; answers are not available until graded' };
  }
  const version = await ScoreVersion.findByPk(exam.finalScoreVersionId);
  const { Certificate } = require('../models');
  const cert = await Certificate.findOne({ where: { examId } });
  return serializeResult(exam, version, cert ? serializeCert(cert, now) : null, now);
}

async function sameOrg(user, otherUserId) {
  const other = await User.findByPk(otherUserId);
  return !!other && other.organizationId === user.organizationId;
}

async function listScoreHistory(examId, user) {
  const exam = await Exam.findByPk(examId);
  if (!exam) throw notFound('exam not found');
  if (
    exam.userId !== user.id &&
    user.role !== 'admin' &&
    !(user.role === 'supervisor' && (await sameOrg(user, exam.userId)))
  ) {
    throw forbidden();
  }
  const rows = await ScoreVersion.findAll({ where: { examId }, order: [['version', 'ASC']] });
  return rows.map((v) => serializeVersion(v, exam));
}

async function listStudentExams(userId, now) {
  const exams = await Exam.findAll({
    where: { userId },
    include: [{ model: Paper, attributes: ['id', 'title'] }],
    order: [['startedAt', 'DESC']],
  });
  return exams.map((e) => ({
    id: e.id,
    paperId: e.paperId,
    paperTitle: e.Paper && e.Paper.title,
    status: e.status,
    startedAt: e.startedAt.toISOString(),
    deadlineAt: e.deadlineAt.toISOString(),
    submittedAt: e.submittedAt ? e.submittedAt.toISOString() : null,
  }));
}

// Supervisor query, restricted by organization authorization.
async function listOrgExams(supervisor, { userId, status } = {}) {
  if (supervisor.role !== 'admin' && supervisor.role !== 'supervisor') throw forbidden();
  const where = {};
  if (status) where.status = status;
  const userWhere = {};
  if (supervisor.role === 'supervisor') userWhere.organizationId = supervisor.organizationId;
  if (userId) userWhere.id = userId;

  const users = await User.findAll({ where: userWhere, attributes: ['id', 'organizationId'] });
  if (userId && users.length === 0) return [];
  where.userId = { [Op.in]: users.map((u) => u.id) };
  if (where.userId[Op.in].length === 0) return [];

  const exams = await Exam.findAll({
    where,
    include: [{ model: Paper, attributes: ['id', 'title'] }],
    order: [['startedAt', 'DESC']],
    limit: 200,
  });
  return exams.map((e) => ({
    id: e.id,
    userId: e.userId,
    paperId: e.paperId,
    paperTitle: e.Paper && e.Paper.title,
    status: e.status,
    startedAt: e.startedAt.toISOString(),
    submittedAt: e.submittedAt ? e.submittedAt.toISOString() : null,
  }));
}

module.exports = {
  startExam,
  getTakingView,
  submitExam,
  expireIfDue,
  sweepExpired,
  reviewExam,
  getResult,
  listScoreHistory,
  listStudentExams,
  listOrgExams,
  loadTakingView,
};
