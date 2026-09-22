'use strict';

const activityService = require('../services/activityService');

async function record(req, res, next) {
  try {
    const data = await activityService.record(req.user, req.params.userId, req.body);
    res.status(201).json({ data });
  } catch (err) { next(err); }
}

async function listForUser(req, res, next) {
  try {
    res.json({ data: await activityService.listForUser(req.user, req.params.userId) });
  } catch (err) { next(err); }
}

module.exports = { record, listForUser };
