const express = require('express');
const examService = require('../services/examService');
const wrongQuestionService = require('../services/wrongQuestionService');
const { computeMastery } = require('../services/masteryService');
const certService = require('../services/certificateService');
const { User } = require('../models');
const { requireRole } = require('../middleware/auth');
const { forbidden } = require('../utils/errors');

const router = express.Router();

// Start an attempt. Daily limit (org-timezone, 3/day) and concurrency are
// enforced inside the service transaction.
router.post('/exams', async (req, res, next) => {
  try {
    const paperId = Number(req.body.paperId);
    if (!Number.isInteger(paperId)) return res.status(400).json({ error: { code: 'BAD_REQUEST', message: 'paperId required' } });
    const view = await examService.startExam({ user: req.user, paperId, now: req.now });
    res.status(201).json(view);
  } catch (e) {
    next(e);
  }
});

// Student's own exam taking view (no answers).
router.get('/exams/:id', async (req, res, next) => {
  try {
    res.json(await examService.getTakingView(Number(req.params.id), req.user));
  } catch (e) {
    next(e);
  }
});

// Submit with idempotency key. Header X-Request-Id or body.requestId.
router.post('/exams/:id/submit', async (req, res, next) => {
  try {
    const requestId = req.headers['x-request-id'] || req.body.requestId;
    const out = await examService.submitExam({
      user: req.user,
      examId: Number(req.params.id),
      requestId,
      answers: req.body.answers,
      now: req.now,
    });
    res.status(out.replay || out.alreadyTerminal ? 200 : 201).json({
      idempotentReplay: !!out.replay,
      alreadyTerminal: !!out.alreadyTerminal,
      timedOut: !!out.timedOut,
      ...out.result,
    });
  } catch (e) {
    next(e);
  }
});

// Explicit timeout: terminalize when past deadline (idempotent).
router.post('/exams/:id/timeout', async (req, res, next) => {
  try {
    const out = await examService.expireIfDue(Number(req.params.id), req.now, { user: req.user });
    if (!out.changed && !out.result) {
      return res.status(409).json({
        error: { code: 'NOT_DUE', message: `exam still within time; deadline ${out.deadlineAt}` },
      });
    }
    res.json({ changed: out.changed, ...(out.result ? { result: out.result } : {}) });
  } catch (e) {
    next(e);
  }
});

// Graded result — answers are returned only after a terminal state exists.
router.get('/exams/:id/result', async (req, res, next) => {
  try {
    res.json(await examService.getResult(Number(req.params.id), req.user, req.now));
  } catch (e) {
    next(e);
  }
});

// Append-only score history (original + every review version).
router.get('/exams/:id/history', async (req, res, next) => {
  try {
    res.json({ versions: await examService.listScoreHistory(Number(req.params.id), req.user) });
  } catch (e) {
    next(e);
  }
});

// Review: create a NEW score version; mandatory reason. Re-checks cert.
router.post('/exams/:id/review', requireRole('supervisor', 'admin'), async (req, res, next) => {
  try {
    const out = await examService.reviewExam({
      reviewer: req.user,
      examId: Number(req.params.id),
      score: Number(req.body.score),
      reason: req.body.reason,
      now: req.now,
    });
    res.status(201).json(out);
  } catch (e) {
    next(e);
  }
});

// Student's own attempt list.
router.get('/me/exams', async (req, res, next) => {
  try {
    res.json({ exams: await examService.listStudentExams(req.user.id, req.now) });
  } catch (e) {
    next(e);
  }
});

// Current mastery with explicit decay boundary metadata.
router.get('/me/mastery', async (req, res, next) => {
  try {
    const m = await computeMastery(req.user.id, req.now, { organizationId: req.user.organizationId });
    res.json(m);
  } catch (e) {
    next(e);
  }
});

router.get('/me/certificates', async (req, res, next) => {
  try {
    res.json({ certificates: await certService.listForUser(req.user.id, req.now) });
  } catch (e) {
    next(e);
  }
});

router.get('/me/wrong-questions', async (req, res, next) => {
  try {
    res.json({ wrongQuestions: await wrongQuestionService.listWrongQuestions({ user: req.user }) });
  } catch (e) {
    next(e);
  }
});

router.get('/exams/:id/wrong-questions', async (req, res, next) => {
  try {
    res.json({
      wrongQuestions: await wrongQuestionService.listWrongQuestions({
        user: req.user,
        examId: Number(req.params.id),
      }),
    });
  } catch (e) {
    next(e);
  }
});

// Admin maintenance: terminalize every in-progress exam past its deadline.
// The same single-terminal-state guarantee applies (row lock + idempotent).
router.post('/admin/sweep-expired', requireRole('admin'), async (req, res, next) => {
  try {
    res.json(await examService.sweepExpired(req.now));
  } catch (e) {
    next(e);
  }
});

// Supervisor queries: organization-scoped. Students are rejected.
router.get('/exams', requireRole('supervisor', 'admin'), async (req, res, next) => {
  try {
    res.json({
      exams: await examService.listOrgExams(req.user, {
        userId: req.query.userId ? Number(req.query.userId) : undefined,
        status: req.query.status,
      }),
    });
  } catch (e) {
    next(e);
  }
});

// Supervisor checking a student's mastery within their organization.
router.get('/users/:id/mastery', requireRole('supervisor', 'admin'), async (req, res, next) => {
  try {
    const target = await User.findByPk(Number(req.params.id));
    if (!target) return res.status(404).json({ error: { code: 'NOT_FOUND', message: 'user not found' } });
    if (req.user.role === 'supervisor' && target.organizationId !== req.user.organizationId) {
      throw forbidden('supervisor can only query users in their own organization');
    }
    const m = await computeMastery(target.id, req.now, { organizationId: target.organizationId });
    res.json({ userId: target.id, ...m });
  } catch (e) {
    next(e);
  }
});

module.exports = router;
