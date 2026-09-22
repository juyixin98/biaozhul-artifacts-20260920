const { DataTypes } = require('sequelize');

// Append-only score history for one exam. Grading creates version 1;
// every review (复核) appends a new version with a reason. Existing rows
// are never updated or deleted — historical scores are immutable.
// The exam's current effective result always points at the highest version.
module.exports = (sequelize) =>
  sequelize.define(
    'ScoreVersion',
    {
      id: { type: DataTypes.BIGINT.UNSIGNED, primaryKey: true, autoIncrement: true },
      examId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      version: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      score: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false }, // 0..100
      correctCount: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false },
      wrongCount: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false },
      gradingDetail: { type: DataTypes.JSON, allowNull: false },
      // "auto" for server grading; "review" for manual re-score.
      source: { type: DataTypes.ENUM('auto', 'review'), allowNull: false },
      reason: { type: DataTypes.STRING(500), allowNull: true },
      reviewerId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: true },
      createdAt: { type: DataTypes.DATE(3), allowNull: false },
    },
    {
      tableName: 'score_versions',
      updatedAt: false,
      indexes: [
        { unique: true, fields: ['exam_id', 'version'] },
        { fields: ['exam_id'] },
      ],
    }
  );
