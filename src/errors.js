'use strict';

class HttpError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const badRequest = (msg) => new HttpError(400, 'BAD_REQUEST', msg);
const unauthorized = (msg = '未认证') => new HttpError(401, 'UNAUTHORIZED', msg);
const forbidden = (msg = '无权访问') => new HttpError(403, 'FORBIDDEN', msg);
const notFound = (msg = '资源不存在') => new HttpError(404, 'NOT_FOUND', msg);
const conflict = (msg) => new HttpError(409, 'CONFLICT', msg);
const locked = (msg) => new HttpError(423, 'LOCKED', msg);

module.exports = { HttpError, badRequest, unauthorized, forbidden, notFound, conflict, locked };
