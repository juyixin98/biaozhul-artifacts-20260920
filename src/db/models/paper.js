'use strict';

// 试卷：按 orgId + seed 幂等生成（同组织同种子返回同一份）
module.exports = (sequelize, DataTypes) =>
  sequelize.define('Paper', {
    id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
    orgId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    seed: { type: DataTypes.STRING(64), allowNull: false },
    title: { type: DataTypes.STRING(128), allowNull: false },
    questionCount: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    // 评分规则快照：每题分值与总分，生成后冻结
    scoringRule: { type: DataTypes.JSON, allowNull: false },
  }, {
    tableName: 'papers',
    underscored: true,
    indexes: [{ unique: true, fields: ['org_id', 'seed'] }],
  });
