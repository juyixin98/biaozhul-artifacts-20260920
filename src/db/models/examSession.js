'use strict';

// 考试场次（一次“开始考试”）。状态机：
// in_progress -> graded（正常提交/超时自动交卷后的唯一终态）
// 终态不可再变，超时与提交并发只能产生一个终态。
module.exports = (sequelize, DataTypes) =>
  sequelize.define('ExamSession', {
    id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
    orgId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    paperId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    userId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    status: { type: DataTypes.ENUM('in_progress', 'graded'), allowNull: false, defaultValue: 'in_progress' },
    startedAt: { type: DataTypes.DATE(3), allowNull: false },
    // 截止时间 = 开始 + 30 分钟（明确边界：startedAt <= t < deadlineAt 为可作答窗口）
    deadlineAt: { type: DataTypes.DATE(3), allowNull: false },
    submittedAt: { type: DataTypes.DATE(3), allowNull: true },
    finalizedAt: { type: DataTypes.DATE(3), allowNull: true },
    finalReason: { type: DataTypes.ENUM('submit', 'timeout'), allowNull: true },
    currentScoreVersionId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: true },
  }, {
    tableName: 'exam_sessions',
    underscored: true,
    indexes: [{ fields: ['user_id', 'started_at'] }],
  });
