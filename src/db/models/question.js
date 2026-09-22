'use strict';

// 题库。题型：single 单选 / multiple 多选 / judge 判断
// 编辑本表不影响已冻结试卷——试卷持有的是 paper_questions 快照
module.exports = (sequelize, DataTypes) =>
  sequelize.define('Question', {
    id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
    orgId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    type: { type: DataTypes.ENUM('single', 'multiple', 'judge'), allowNull: false },
    difficulty: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false, validate: { min: 1, max: 5 } },
    stem: { type: DataTypes.TEXT, allowNull: false },
    options: { type: DataTypes.JSON, allowNull: true }, // [{key:'A',text:'...'}]；判断题为 null
    // 正确答案：单选 'A'；多选 ['A','C']；判断 true/false
    answer: { type: DataTypes.JSON, allowNull: false },
    explanation: { type: DataTypes.TEXT, allowNull: true },
    points: { type: DataTypes.DECIMAL(5, 2), allowNull: false, defaultValue: 5 },
    active: { type: DataTypes.BOOLEAN, allowNull: false, defaultValue: true },
  }, { tableName: 'questions', underscored: true });
