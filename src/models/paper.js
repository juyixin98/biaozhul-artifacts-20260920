const { DataTypes } = require('sequelize');
const config = require('../config');

// A frozen exam paper. The selection seed deterministically decides which
// questions were picked; from the moment the paper is generated its
// question texts, answers, explanations and scoring rules live in
// paper_questions and are immutable.
module.exports = (sequelize) =>
  sequelize.define(
    'Paper',
    {
      id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      organizationId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: true },
      title: { type: DataTypes.STRING(191), allowNull: false },
      // Deterministic RNG seed used for question selection.
      selectionSeed: { type: DataTypes.STRING(64), allowNull: false },
      questionCount: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false, defaultValue: config.PAPER_QUESTION_COUNT },
      minHardRatio: {
        type: DataTypes.DECIMAL(4, 3),
        allowNull: false,
        defaultValue: config.PAPER_HARD_RATIO,
      },
      pointsPerQuestion: {
        type: DataTypes.TINYINT.UNSIGNED,
        allowNull: false,
        defaultValue: config.POINTS_PER_QUESTION,
      },
      durationMinutes: {
        type: DataTypes.TINYINT.UNSIGNED,
        allowNull: false,
        defaultValue: config.EXAM_DURATION_MINUTES,
      },
      status: {
        type: DataTypes.ENUM('active', 'retired'),
        allowNull: false,
        defaultValue: 'active',
      },
    },
    {
      tableName: 'papers',
      indexes: [{ fields: ['organization_id'] }],
    }
  );
