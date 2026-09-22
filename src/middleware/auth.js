const { User } = require('../models');
const { unauthorized, forbidden } = require('../utils/errors');

// Simplified authentication for the assessment backend: the client presents
// x-user-id; the acting user is loaded and attached to the request.
// Authorization (student-self / supervisor-org) is enforced in the routes.
async function authRequired(req, _res, next) {
  try {
    const raw = req.headers['x-user-id'];
    if (!raw) throw unauthorized();
    const userId = Number(raw);
    if (!Number.isInteger(userId) || userId <= 0) throw unauthorized();
    const user = await User.findByPk(userId);
    if (!user) throw unauthorized();
    req.user = user;
    next();
  } catch (err) {
    next(err);
  }
}

function requireRole(...roles) {
  return (req, _res, next) => {
    if (!req.user || !roles.includes(req.user.role)) {
      return next(forbidden(`requires role: ${roles.join(' or ')}`));
    }
    next();
  };
}

module.exports = { authRequired, requireRole };
