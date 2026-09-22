'use strict';

const { hashAnswers, normalizeAnswer } = require('../../utils/answers');

// 交卷请求：requestId 全局唯一，实现幂等与冲突检测。
// - 同 requestId + 同内容（answersHash 相同）：返回原成绩
// - 同 requestId + 不同内容：409 冲突
function model(sequelize, DataTypes) {
  const Submission = sequelize.define('Submission', {
    id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
    sessionId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    requestId: { type: DataTypes.STRING(64), allowNull: false, unique: true },
    answersHash: { type: DataTypes.STRING(64), allowNull: false },
    answers: { type: DataTypes.JSON, allowNull: false },
    clientSubmittedAt: { type: DataTypes.DATE(3), allowNull: true },
    // arrived 截止前到达；late 截止后到达（按已作答内容判超时交卷）
    arrival: { type: DataTypes.ENUM('arrived', 'late'), allowNull: false },
    duplicateOfId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: true },
  }, { tableName: 'submissions', underscored: true });

  Submission.hashAnswers = hashAnswers;
  Submission.normalizeAnswer = normalizeAnswer;
  return Submission;
}

module.exports = model;
