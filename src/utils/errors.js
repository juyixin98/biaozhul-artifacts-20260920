const config = require('../config');

class HttpError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const badRequest = (msg, code = 'BAD_REQUEST') => new HttpError(400, code, msg);
const unauthorized = (msg = 'authentication required') => new HttpError(401, 'UNAUTHORIZED', msg);
const forbidden = (msg = 'forbidden') => new HttpError(403, 'FORBIDDEN', msg);
const notFound = (msg = 'not found') => new HttpError(404, 'NOT_FOUND', msg);
const conflict = (msg, code = 'CONFLICT') => new HttpError(409, code, msg);
const unprocessable = (msg, code = 'UNPROCESSABLE') => new HttpError(422, code, msg);

module.exports = { HttpError, badRequest, unauthorized, forbidden, notFound, conflict, unprocessable };
