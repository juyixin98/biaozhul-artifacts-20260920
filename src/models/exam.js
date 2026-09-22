const { DataTypes } = require('sequelize');

// One exam attempt. State machine: in_progress -> submitted | expired.
// Only ONE terminal row state may ever exist per attempt — enforced by a
// transaction with row lock plus the unique constraint on submissions.
module.exports = (sequelize) =>
  sequelize.define(
    'Exam',
    {
      id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      paperId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      userId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      startedAt: { type: DataTypes.DATE(3), allowNull: false },
      deadlineAt: { type: DataTypes.DATE(3), allowNull: false },
      submittedAt: { type: DataTypes.DATE(3), allowNull: true },
      // Deterministic shuffle seed for question/option ordering presented
      // to this student (does not affect grading).
      presentationSeed: { type: DataTypes.STRING(64), allowNull: false },
      status: {
        type: DataTypes.ENUM('in_progress', 'submitted', 'expired'),
        allowNull: false,
        defaultValue: 'in_progress',
      },
      finalScoreVersionId: { type: DataTypes.BIGINT.UNSIGNED, allowNull: true },
    },
    {
      tableName: 'exams',
      indexes: [
        { fields: ['user_id', 'status'] },
        { fields: ['paper_id'] },
      ],
    }
  );
