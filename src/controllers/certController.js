'use strict';

const certService = require('../services/certificateService');

async function issue(req, res, next) {
  try {
    const data = await certService.issue(req.user, req.params.sessionId);
    res.status(201).json({ data });
  } catch (err) { next(err); }
}

async function mine(req, res, next) {
  try {
    res.json({ data: await certService.listForUser(req.user) });
  } catch (err) { next(err); }
}

async function listOrg(req, res, next) {
  try {
    res.json({ data: await certService.listOrg(req.user) });
  } catch (err) { next(err); }
}

module.exports = { issue, mine, listOrg };
