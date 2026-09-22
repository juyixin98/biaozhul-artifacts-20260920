'use strict';

// 证书：同一场考试最多一张（session_id 唯一），有效期 12 个月。
// status：issued 有效 / revoked 复核降分后失效；过期由 validUntil 即时判定，不落状态。
module.exports = (sequelize, DataTypes) =>
  sequelize.define('Certificate', {
    id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
    certNumber: { type: DataTypes.STRING(64), allowNull: false, unique: true },
    orgId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    userId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    sessionId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false, unique: true },
    score: { type: DataTypes.DECIMAL(5, 2), allowNull: false },
    masteryAtIssue: { type: DataTypes.DECIMAL(5, 2), allowNull: false },
    issuedAt: { type: DataTypes.DATE(3), allowNull: false },
    validUntil: { type: DataTypes.DATE(3), allowNull: false }, // issuedAt + 12 个自然月
    status: { type: DataTypes.ENUM('issued', 'revoked'), allowNull: false, defaultValue: 'issued' },
    revokedReason: { type: DataTypes.STRING(255), allowNull: true },
  }, { tableName: 'certificates', underscored: true });
