'use strict';

const { HttpError } = require('../errors');

// 统一错误响应：不泄露堆栈与 SQL；业务错误带稳定 code
// eslint-disable-next-line no-unused-vars
module.exports = function errorHandler(err, req, res, next) {
  if (err instanceof HttpError) {
    return res.status(err.status).json({ error: { code: err.code, message: err.message } });
  }
  if (err && err.name === 'SequelizeValidationError') {
    return res.status(400).json({ error: { code: 'VALIDATION_ERROR', message: err.errors.map((e) => e.message).join('; ') } });
  }
  req.app.locals.logger?.(err);
  return res.status(500).json({ error: { code: 'INTERNAL_ERROR', message: '服务器内部错误' } });
};
