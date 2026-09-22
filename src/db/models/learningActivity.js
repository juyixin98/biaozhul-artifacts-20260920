'use strict';

// 学习活动：交卷出分自动记一条（type=exam）；主管也可登记培训/练习等活动。
// 掌握度下降计时以“最近一次学习活动”为准，明确 30 天边界。
module.exports = (sequelize, DataTypes) =>
  sequelize.define('LearningActivity', {
    id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
    orgId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    userId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    type: { type: DataTypes.STRING(32), allowNull: false, defaultValue: 'exam' },
    refId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: true },
    occurredAt: { type: DataTypes.DATE(3), allowNull: false },
    note: { type: DataTypes.STRING(255), allowNull: true },
  }, {
    tableName: 'learning_activities',
    underscored: true,
    indexes: [{ fields: ['user_id', 'occurred_at'] }],
  });
