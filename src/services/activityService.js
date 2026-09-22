'use strict';

const { LearningActivity } = require('../db');
const clock = require('../config/clock');
const { badRequest, notFound, forbidden } = require('../errors');
const { User } = require('../db');

// 主管为学员登记学习活动（培训/练习等），刷新掌握度下降计时
async function record(actor, userId, body) {
  const target = await User.findByPk(userId);
  if (!target || target.orgId !== actor.orgId) throw notFound('学员不存在');
  if (!body.type) throw badRequest('type 必填');
  const occurredAt = body.occurredAt ? new Date(body.occurredAt) : clock.now();
  if (Number.isNaN(occurredAt.getTime())) throw badRequest('occurredAt 非法时间');
  const activity = await LearningActivity.create({
    orgId: actor.orgId, userId, type: body.type, refId: body.refId || null, occurredAt, note: body.note || null,
  });
  return activity;
}

async function listForUser(actor, targetUserId) {
  if (actor.role === 'student' && actor.id !== Number(targetUserId)) throw forbidden('只能查看自己的学习记录');
  const target = await User.findByPk(targetUserId);
  if (!target || target.orgId !== actor.orgId) throw notFound('用户不存在');
  return LearningActivity.findAll({ where: { userId: targetUserId }, order: [['occurredAt', 'DESC']], limit: 100 });
}

module.exports = { record, listForUser };
