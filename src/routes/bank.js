const express = require('express');
const { Question } = require('../models');
const paperService = require('../services/paperService');
const { requireRole } = require('../middleware/auth');
const { notFound } = require('../utils/errors');

const questionRouter = express.Router();
const paperRouter = express.Router();

// Question bank management (supervisors/admins only). Students never see
// answers via bank endpoints; they only receive graded-result detail.
questionRouter.get('/', requireRole('supervisor', 'admin'), async (req, res, next) => {
  try {
    const where = { active: true };
    if (req.query.type) where.type = req.query.type;
    if (req.query.difficulty) where.difficulty = Number(req.query.difficulty);
    const rows = await Question.findAll({ where, order: [['id', 'ASC']], limit: 500 });
    res.json({
      questions: rows.map((q) => ({
        id: q.id,
        organizationId: q.organizationId,
        type: q.type,
        difficulty: q.difficulty,
        stem: q.stem,
        options: q.options,
        answer: q.answer,
        explanation: q.explanation,
      })),
    });
  } catch (e) {
    next(e);
  }
});

questionRouter.post('/', requireRole('supervisor', 'admin'), async (req, res, next) => {
  try {
    const q = await paperService.createQuestion({
      ...req.body,
      organizationId: req.user.role === 'admin' ? req.body.organizationId ?? null : req.user.organizationId,
    });
    res.status(201).json({ id: q.id });
  } catch (e) {
    next(e);
  }
});

questionRouter.put('/:id', requireRole('supervisor', 'admin'), async (req, res, next) => {
  try {
    const q = await paperService.updateQuestion(Number(req.params.id), req.body);
    if (!q) throw notFound('question not found');
    res.json({ id: q.id, updated: true });
  } catch (e) {
    next(e);
  }
});

// Generate a deterministic frozen paper from the current bank.
paperRouter.post('/', requireRole('supervisor', 'admin'), async (req, res, next) => {
  try {
    const organizationId =
      req.user.role === 'admin' ? req.body.organizationId ?? null : req.user.organizationId;
    const paper = await paperService.generatePaper({
      organizationId,
      title: req.body.title,
      seed: req.body.seed,
      options: { count: req.body.count, minHardRatio: req.body.minHardRatio },
    });
    res.status(201).json(serializePaperAdmin(paper));
  } catch (e) {
    next(e);
  }
});

paperRouter.get('/:id', requireRole('supervisor', 'admin'), async (req, res, next) => {
  try {
    const paper = await paperService.getPaper(Number(req.params.id));
    if (!paper) throw notFound('paper not found');
    res.json(serializePaperAdmin(paper));
  } catch (e) {
    next(e);
  }
});

function serializePaperAdmin(paper) {
  return {
    id: paper.id,
    organizationId: paper.organizationId,
    title: paper.title,
    selectionSeed: paper.selectionSeed,
    questionCount: paper.questionCount,
    minHardRatio: Number(paper.minHardRatio),
    pointsPerQuestion: paper.pointsPerQuestion,
    durationMinutes: paper.durationMinutes,
    status: paper.status,
    questions: paper.questions.map((q) => ({
      paperQuestionId: q.id,
      sourceQuestionId: q.sourceQuestionId,
      position: q.position,
      type: q.type,
      difficulty: q.difficulty,
      stem: q.stem,
      options: q.options,
      answer: q.answer,
      explanation: q.explanation,
      points: q.points,
      scoringRule: q.scoringRule,
    })),
  };
}

module.exports = { questionRouter, paperRouter };
