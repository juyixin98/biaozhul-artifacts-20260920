'use strict';

const { User } = require('../db');
const { unauthorized, forbidden } = require('../errors');

// 极简 Bearer Token 认证：Authorization: Bearer <apiToken>
async function authenticate(req, res, next) {
  try {
    const header = req.headers.authorization || '';
    const token = header.startsWith('Bearer ') ? header.slice(7) : (req.headers['x-api-token'] || '');
    if (!token) throw unauthorized('缺少认证令牌');
    const user = await User.findOne({ where: { apiToken: token }, include: [{ association: 'organization' }] });
    if (!user) throw unauthorized('令牌无效');
    req.user = user;
    next();
  } catch (err) {
    next(err);
  }
}

const requireRole = (...roles) => (req, res, next) => {
  if (!req.user || !roles.includes(req.user.role)) return next(forbidden('角色无权执行该操作'));
  return next();
};

module.exports = { authenticate, requireRole };
