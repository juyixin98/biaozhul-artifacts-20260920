const { Op } = require('sequelize');
const {
  sequelize,
  Question,
  Paper,
  PaperQuestion,
} = require('../models');
const config = require('../config');
const { mulberry32, shuffle } = require('../utils/time');
const { unprocessable, badRequest } = require('../utils/errors');

const TYPES = ['single', 'multiple', 'boolean'];

function validateQuestionPayload(payload, { partial = false } = {}) {
  const out = {};
  if (!partial || 'type' in payload) {
    if (!TYPES.includes(payload.type)) throw badRequest('type must be one of single/multiple/boolean');
    out.type = payload.type;
  }
  if (!partial || 'difficulty' in payload) {
    const d = Number(payload.difficulty);
    if (!Number.isInteger(d) || d < 1 || d > 5) throw badRequest('difficulty must be an integer 1..5');
    out.difficulty = d;
  }
  if (!partial || 'stem' in payload) {
    if (!payload.stem || typeof payload.stem !== 'string') throw badRequest('stem is required');
    out.stem = payload.stem;
  }
  if (!partial || 'explanation' in payload) {
    out.explanation = payload.explanation ? String(payload.explanation) : '';
  }

  const type = out.type || payload.type;
  if (type === 'boolean') {
    out.options = ['true', 'false'];
    const ans = normalizeAnswer(payload.answer);
    if (ans.length !== 1 || !['true', 'false'].includes(ans[0])) {
      throw badRequest('boolean answer must be ["true"] or ["false"]');
    }
    out.answer = ans;
  } else if (type === 'single' || type === 'multiple') {
    if (!partial || 'options' in payload) {
      const options = payload.options;
      if (!Array.isArray(options) || new Set(options).size < 2 || options.some((o) => !String(o).trim())) {
        throw badRequest('options must be an array of at least 2 unique non-empty strings');
      }
      out.options = options.map(String);
    }
    const options = out.options || payload.options;
    if (options) {
      const ans = normalizeAnswer(payload.answer);
      if (ans.length === 0) throw badRequest('answer is required');
      if (type === 'single' && ans.length !== 1) throw badRequest('single-choice answer must contain exactly one option');
      if (type === 'multiple' && ans.length < 2) throw badRequest('multiple-choice answer must contain at least two options');
      if (ans.some((a) => !options.includes(a))) throw badRequest('answer contains option not present in options');
      out.answer = ans;
    }
  }
  return out;
}

function normalizeAnswer(answer) {
  if (answer == null) return [];
  const list = Array.isArray(answer) ? answer : [answer];
  return [...new Set(list.map(String))].sort();
}

async function createQuestion(payload) {
  const fields = validateQuestionPayload(payload);
  return Question.create({
    organizationId: payload.organizationId ?? null,
    type: fields.type,
    difficulty: fields.difficulty,
    stem: fields.stem,
    options: fields.options,
    answer: fields.answer,
    explanation: fields.explanation,
  });
}

// Edits the LIVE bank only. Existing paper snapshots are independent rows,
// so historical papers stay exactly as they were when generated.
async function updateQuestion(id, payload) {
  const question = await Question.findByPk(id);
  if (!question) return null;
  const merged = {
    type: question.type,
    difficulty: question.difficulty,
    stem: question.stem,
    options: question.options,
    answer: question.answer,
    explanation: question.explanation,
    ...payload,
  };
  const fields = validateQuestionPayload(merged, { partial: false });
  question.set(fields);
  await question.save();
  return question;
}

// ---------- Deterministic paper composition ----------

// Selects `count` questions from the bank using a seeded RNG.
// Guarantees at least minHardRatio of picked questions have difficulty >= 4.
// FAILS EXPLICITLY when the bank is too small or lacks enough hard
// questions — it never pads with duplicates.
function selectQuestions(candidates, { count, minHardRatio, seed }) {
  const hard = candidates.filter((q) => q.difficulty >= 4);
  const needHard = Math.ceil(count * minHardRatio);

  if (candidates.length < count) {
    throw unprocessable(
      `question bank has only ${candidates.length} eligible questions, paper requires ${count}; refuse to reuse questions`,
      'INSUFFICIENT_QUESTIONS'
    );
  }
  if (hard.length < needHard) {
    throw unprocessable(
      `only ${hard.length} questions with difficulty >= 4 available, paper requires at least ${needHard} (${Math.round(
        minHardRatio * 100
      )}% of ${count}); add more hard questions`,
      'INSUFFICIENT_HARD_QUESTIONS'
    );
  }

  const rng = mulberry32(seed);
  // The RNG call order is fixed, so identical (bank state, seed) always
  // produces the identical paper.
  const pickedHard = shuffle(hard, rng).slice(0, needHard);
  const hardIds = new Set(pickedHard.map((q) => q.id));
  const restPool = candidates.filter((q) => !hardIds.has(q.id));
  const pickedRest = shuffle(restPool, rng).slice(0, count - needHard);
  return shuffle([...pickedHard, ...pickedRest], rng);
}

async function generatePaper({ organizationId = null, title, seed, options = {} }) {
  const count = options.count || config.PAPER_QUESTION_COUNT;
  const minHardRatio = options.minHardRatio ?? config.PAPER_HARD_RATIO;
  const selectionSeed = seed || `paper-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

  const candidates = await Question.findAll({
    where: {
      active: true,
      // An organization can compose from its own bank PLUS the shared
      // bank (organization_id IS NULL); platform admin uses shared only.
      // NOTE: SQL `IN (x, NULL)` never matches NULL, hence explicit OR.
      ...(organizationId
        ? { [Op.or]: [{ organizationId }, { organizationId: null }] }
        : { organizationId: null }),
    },
    order: [['id', 'ASC']], // deterministic input order before the seeded shuffle
  });

  const picked = selectQuestions(candidates, { count, minHardRatio, seed: selectionSeed });

  return sequelize.transaction(async (t) => {
    const paper = await Paper.create(
      {
        organizationId,
        title: title || `Competency Paper ${new Date().toISOString().slice(0, 10)}`,
        selectionSeed,
        questionCount: count,
        minHardRatio,
        pointsPerQuestion: config.POINTS_PER_QUESTION,
        durationMinutes: config.EXAM_DURATION_MINUTES,
      },
      { transaction: t }
    );

    await PaperQuestion.bulkCreate(
      picked.map((q, idx) => ({
        paperId: paper.id,
        position: idx + 1,
        sourceQuestionId: q.id,
        type: q.type,
        difficulty: q.difficulty,
        stem: q.stem,
        options: q.options,
        answer: q.answer, // frozen on the snapshot; never read from the bank again
        explanation: q.explanation,
        points: config.POINTS_PER_QUESTION,
        scoringRule: 'exact',
      })),
      { transaction: t }
    );

    return getPaper(paper.id, t);
  });
}

async function getPaper(id, transaction = null) {
  const paper = await Paper.findByPk(id, {
    include: [{ model: PaperQuestion, as: 'questions', separate: true, order: [['position', 'ASC']] }],
    transaction,
  });
  return paper;
}

module.exports = {
  validateQuestionPayload,
  normalizeAnswer,
  createQuestion,
  updateQuestion,
  selectQuestions,
  generatePaper,
  getPaper,
};
