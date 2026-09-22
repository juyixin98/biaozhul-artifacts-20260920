'use strict';

const { getMastery } = require('../services/masteryService');
const { User } = require('../db');
const { notFound } = require('../errors');

async function mine(req, res, next) {
  try {
    res.json({ data: await getMastery(req.user.id) });
  } catch (err) { next(err); }
}

async function forUser(req, res, next) {
  try {
    const target = await User.findByPk(req.params.userId);
    if (!target || target.orgId !== req.user.orgId) throw notFound('用户不存在');
    res.json({ data: await getMastery(target.id) });
  } catch (err) { next(err); }
}

module.exports = { mine, forUser };
