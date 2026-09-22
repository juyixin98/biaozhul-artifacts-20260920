const { HttpError } = require('../utils/errors');

function errorHandler(err, _req, res, _next) {
  if (err instanceof HttpError) {
    return res.status(err.status).json({ error: { code: err.code, message: err.message } });
  }
  // Unique constraint violations are surfaced as conflicts (idempotency
  // races are normally handled by the services, this is the safety net).
  if (err && err.name === 'SequelizeUniqueConstraintError') {
    return res.status(409).json({ error: { code: 'CONFLICT', message: 'conflicting resource' } });
  }
  // eslint-disable-next-line no-console
  console.error('[error]', err);
  res.status(500).json({ error: { code: 'INTERNAL', message: 'internal server error' } });
}

module.exports = errorHandler;
