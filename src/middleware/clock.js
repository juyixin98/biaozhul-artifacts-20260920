const config = require('../config');

// Deterministic clock injection for tests. When ALLOW_CLOCK_OVERRIDE=1,
// a request may send `x-now` with an ISO-8601 instant; every business rule
// evaluated in that request (attempt windows, deadlines, mastery decay,
// certificate validity) reads req.now instead of the wall clock.
// The feature is hard-disabled unless the env flag is set, so production
// deployments ignore the header entirely.
function clockMiddleware(req, _res, next) {
  req.now = new Date();
  if (config.allowClockOverride && req.headers['x-now']) {
    const parsed = new Date(req.headers['x-now']);
    if (!Number.isNaN(parsed.getTime())) {
      req.now = parsed;
    }
  }
  next();
}

module.exports = clockMiddleware;
