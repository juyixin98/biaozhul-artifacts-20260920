const { DataTypes } = require('sequelize');

// Immutable per-paper snapshot of a question. Writing the answer and
// explanation here (instead of referencing questions) is what guarantees a
// frozen paper: later edits to the question bank cannot reach this row.
module.exports = (sequelize) =>
  sequelize.define(
    'PaperQuestion',
    {
      id: { type: DataTypes.BIGINT.UNSIGNED, primaryKey: true, autoIncrement: true },
      paperId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      position: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false }, // 1..20
      sourceQuestionId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: true },
      type: {
        type: DataTypes.ENUM('single', 'multiple', 'boolean'),
        allowNull: false,
      },
      difficulty: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false },
      stem: { type: DataTypes.TEXT, allowNull: false },
      options: { type: DataTypes.JSON, allowNull: false },
      answer: { type: DataTypes.JSON, allowNull: false },
      explanation: { type: DataTypes.TEXT, allowNull: false, defaultValue: '' },
      points: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false },
      // Scoring rule frozen with the paper. "exact" = multiple-choice must
      // match the answer set completely to earn any points (no partial).
      scoringRule: { type: DataTypes.ENUM('exact'), allowNull: false, defaultValue: 'exact' },
    },
    {
      tableName: 'paper_questions',
      indexes: [
        { unique: true, fields: ['paper_id', 'position'] },
        { fields: ['source_question_id'] },
      ],
    }
  );
