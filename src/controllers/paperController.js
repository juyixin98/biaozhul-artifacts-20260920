'use strict';

const paperService = require('../services/paperService');
const clock = require('../config/clock');

async function generate(req, res, next) {
  try {
    const paper = await paperService.generatePaper(req.user.orgId, String(req.body.seed), req.body.title);
    res.status(201).json({ data: paperService.toAdminJson(paper) });
  } catch (err) { next(err); }
}

// 学员获取试卷（开始前/作答用）：不含答案与解析
async function studentView(req, res, next) {
  try {
    const paper = await paperService.getPaper(req.user.orgId, req.params.id);
    res.json({ data: paperService.toStudentJson(paper) });
  } catch (err) { next(err); }
}

// 管理端试卷详情：含冻结答案，用于核对
async function adminView(req, res, next) {
  try {
    const paper = await paperService.getPaper(req.user.orgId, req.params.id);
    res.json({ data: paperService.toAdminJson(paper) });
  } catch (err) { next(err); }
}

module.exports = { generate, studentView, adminView, clock };
