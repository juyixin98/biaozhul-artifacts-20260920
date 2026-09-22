'use strict';

const express = require('express');
const { authenticate, requireRole } = require('../middleware/auth');
const errorHandler = require('../middleware/errorHandler');

const questionController = require('../controllers/questionController');
const paperController = require('../controllers/paperController');
const examController = require('../controllers/examController');
const certController = require('../controllers/certController');
const masteryController = require('../controllers/masteryController');
const activityController = require('../controllers/activityController');

const router = express.Router();

// ---- 题库（管理员/主管）；学员无题库入口，避免接触答案 ----
router.get('/questions', authenticate, requireRole('admin', 'supervisor'), questionController.list);
router.post('/questions', authenticate, requireRole('admin', 'supervisor'), questionController.create);
router.patch('/questions/:id', authenticate, requireRole('admin', 'supervisor'), questionController.update);
router.delete('/questions/:id', authenticate, requireRole('admin', 'supervisor'), questionController.remove);

// ---- 组卷 ----
router.post('/papers', authenticate, requireRole('admin', 'supervisor'), paperController.generate);
router.get('/papers/:id', authenticate, paperController.studentView); // 默认视图不含答案
router.get('/papers/:id/full', authenticate, requireRole('admin', 'supervisor'), paperController.adminView);

// ---- 考试 ----
router.post('/papers/:paperId/exams', authenticate, requireRole('student'), examController.start);
router.post('/exams/:id/submit', authenticate, requireRole('student'), examController.submit);
router.get('/exams/:id/result', authenticate, examController.result); // 学员仅自己；主管组织内
router.get('/exams', authenticate, requireRole('admin', 'supervisor'), examController.listOrgSessions);
router.post('/exams/:id/reviews', authenticate, requireRole('admin', 'supervisor'), examController.review);
router.post('/exams/sweep-timeouts', authenticate, requireRole('admin', 'supervisor'), examController.sweepTimeouts);

// ---- 掌握度 ----
router.get('/mastery', authenticate, masteryController.mine);
router.get('/users/:userId/mastery', authenticate, requireRole('admin', 'supervisor'), masteryController.forUser);

// ---- 证书 ----
router.post('/exams/:sessionId/certificate', authenticate, requireRole('student'), certController.issue);
router.get('/certificates', authenticate, certController.mine);
router.get('/org/certificates', authenticate, requireRole('admin', 'supervisor'), certController.listOrg);

// ---- 学习活动 ----
router.post('/users/:userId/activities', authenticate, requireRole('admin', 'supervisor'), activityController.record);
router.get('/users/:userId/activities', authenticate, activityController.listForUser);

module.exports = router;
