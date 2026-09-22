'use strict';

// 试卷题目快照：组卷时把题干、选项、答案、解析逐字冻结。
// 之后题库（questions）如何编辑都不会改变历史试卷。
module.exports = (sequelize, DataTypes) =>
  sequelize.define('PaperQuestion', {
    id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
    paperId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    questionId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    order: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    type: { type: DataTypes.ENUM('single', 'multiple', 'judge'), allowNull: false },
    difficulty: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false },
    stem: { type: DataTypes.TEXT, allowNull: false },
    options: { type: DataTypes.JSON, allowNull: true },
    answer: { type: DataTypes.JSON, allowNull: false },
    explanation: { type: DataTypes.TEXT, allowNull: true },
    points: { type: DataTypes.DECIMAL(5, 2), allowNull: false },
  }, {
    tableName: 'paper_questions',
    underscored: true,
    indexes: [{ unique: true, fields: ['paper_id', 'order'] }],
  });
