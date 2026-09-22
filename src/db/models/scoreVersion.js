'use strict';

// 成绩版本：仅追加（append-only）。首版来自交卷评分；复核不改旧行，只新增版本。
// 掌握度取“当前有效版本”（isCurrent=1）。
module.exports = (sequelize, DataTypes) =>
  sequelize.define('ScoreVersion', {
    id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
    sessionId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    version: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    score: { type: DataTypes.DECIMAL(5, 2), allowNull: false }, // 百分制
    // 每题判分快照（冻结题目版本 + 学员答案 + 是否正确 + 得分），错题即从其中 correct=false 得到
    detail: { type: DataTypes.JSON, allowNull: false },
    source: { type: DataTypes.ENUM('submit', 'review'), allowNull: false },
    reason: { type: DataTypes.STRING(255), allowNull: true }, // 复核原因（source=review 必填）
    reviewerId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: true },
    isCurrent: { type: DataTypes.BOOLEAN, allowNull: false, defaultValue: false },
  }, {
    tableName: 'score_versions',
    underscored: true,
    indexes: [{ unique: true, fields: ['session_id', 'version'] }],
  });
