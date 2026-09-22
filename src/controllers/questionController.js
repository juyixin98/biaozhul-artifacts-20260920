'use strict';

const { Question } = require('../db');
const { badRequest, forbidden, notFound } = require('../errors');

const TYPES = ['single', 'multiple', 'judge'];

function validateQuestion(body) {
  if (!TYPES.includes(body.type)) throw badRequest('type 必须是 single/multiple/judge');
  const difficulty = Number(body.difficulty);
  if (!Number.isInteger(difficulty) || difficulty < 1 || difficulty > 5) {
    throw badRequest('difficulty 必须为 1–5 的整数');
  }
  if (!body.stem) throw badRequest('stem 必填');
  if (!('answer' in body)) throw badRequest('answer 必填');
  if (body.type === 'single' && typeof body.answer !== 'string') throw badRequest('单选题 answer 为选项字母');
  if (body.type === 'multiple' && !(Array.isArray(body.answer) && body.answer.length)) {
    throw badRequest('多选题 answer 为非空字母数组');
  }
  if (body.type === 'judge' && typeof body.answer !== 'boolean') throw badRequest('判断题 answer 为 true/false');
  if (body.type !== 'judge' && !(Array.isArray(body.options) && body.options.length >= 2)) {
    throw badRequest('选择题至少需要两个选项');
  }
}

// 管理端题目列表（含答案，仅管理员/主管）
async function list(req, res, next) {
  try {
    const questions = await Question.findAll({
      where: { orgId: req.user.orgId },
      order: [['id', 'ASC']],
      limit: 200,
    });
    res.json({ data: questions });
  } catch (err) { next(err); }
}

async function create(req, res, next) {
  try {
    validateQuestion(req.body);
    const q = await Question.create({
      orgId: req.user.orgId,
      type: req.body.type,
      difficulty: Number(req.body.difficulty),
      stem: req.body.stem,
      options: req.body.type === 'judge' ? null : req.body.options,
      answer: req.body.answer,
      explanation: req.body.explanation || null,
      points: req.body.points != null ? Number(req.body.points) : 5,
    });
    res.status(201).json({ data: q });
  } catch (err) { next(err); }
}

// 编辑题库：只影响今后组卷，历史试卷持有的是冻结快照
async function update(req, res, next) {
  try {
    const q = await Question.findOne({ where: { id: req.params.id, orgId: req.user.orgId } });
    if (!q) throw notFound('题目不存在');
    const merged = { type: req.body.type ?? q.type, difficulty: req.body.difficulty ?? q.difficulty,
      stem: req.body.stem ?? q.stem, options: req.body.options ?? q.options,
      answer: req.body.answer ?? q.answer, explanation: req.body.explanation ?? q.explanation,
      points: req.body.points ?? q.points };
    validateQuestion(merged);
    await q.update({
      type: merged.type, difficulty: Number(merged.difficulty), stem: merged.stem,
      options: merged.type === 'judge' ? null : merged.options, answer: merged.answer,
      explanation: merged.explanation, points: Number(merged.points),
    });
    res.json({ data: q });
  } catch (err) { next(err); }
}

async function remove(req, res, next) {
  try {
    const q = await Question.findOne({ where: { id: req.params.id, orgId: req.user.orgId } });
    if (!q) throw notFound('题目不存在');
    await q.update({ active: false }); // 软停用：不破坏历史快照引用
    res.status(204).end();
  } catch (err) { next(err); }
}

module.exports = { list, create, update, remove };
