'use strict';

module.exports = (sequelize, DataTypes) =>
  sequelize.define('User', {
    id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
    orgId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    name: { type: DataTypes.STRING(64), allowNull: false },
    role: { type: DataTypes.ENUM('student', 'supervisor', 'admin'), allowNull: false },
    // 极简认证：登录后下发静态 API 令牌；正式系统应替换为 JWT/会话
    apiToken: { type: DataTypes.STRING(64), allowNull: false, unique: true },
  }, { tableName: 'users', underscored: true });
