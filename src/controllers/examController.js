'use strict';

const examService = require('../services/examService');
const reviewService = require('../services/reviewService');
const { ExamSession } = require('../db');
const { forbidden, notFound } = require('../errors');

async function start(req, res, next) {
  try {
    const data = await examService.startExam(req.user, req.params.paperId);
    res.status(201).json({ data });
  } catch (err) { next(err); }
}

async function submit(req, res, next) {
  try {
    const out = await examService.submit(req.user, req.params.id, req.body);
    res.status(out.idempotent ? 200 : 201).json({
      data: out.result,
      idempotent: out.idempotent, // 同 requestId 同内容的重放标记
    });
  } catch (err) { next(err); }
}

async function result(req, res, next) {
  try {
    const data = await examService.getResult(req.user, req.params.id);
    res.json({ data });
  } catch (err) { next(err); }
}

// 主管：按组织查询考试（学员在 service 层被限制为只能看自己）
async function listOrgSessions(req, res, next) {
  try {
    const where = { orgId: req.user.orgId };
    if (req.query.userId) where.userId = req.query.userId;
    if (req.query.status) where.status = req.query.status;
    const rows = await ExamSession.findAll({ where, order: [['startedAt', 'DESC']], limit: 200 });
    res.json({ data: rows });
  } catch (err) { next(err); }
}

async function review(req, res, next) {
  try {
    const data = await reviewService.reviewSession(req.user, req.params.id, req.body);
    res.json({ data });
  } catch (err) { next(err); }
}

// 超时扫描：把所有已过截止时间仍在进行中的考试置为终态（可由定时器调用）
async function sweepTimeouts(req, res, next) {
  try {
    const data = await examService.finalizeExpired(undefined, { orgId: req.user.orgId });
    res.json({ data: { finalized: data } });
  } catch (err) { next(err); }
}

module.exports = { start, submit, result, listOrgSessions, review, sweepTimeouts };
