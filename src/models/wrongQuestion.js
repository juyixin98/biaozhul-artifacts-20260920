const { DataTypes } = require('sequelize');

// Questions answered incorrectly on a given attempt, stored AGAINST THE
// SNAPSHOT VERSION used by that paper. Wrong-question review therefore
// always shows the exact stem/answer/explanation the student was graded
// against, even if the question bank changed afterwards.
module.exports = (sequelize) =>
  sequelize.define(
    'WrongQuestion',
    {
      id: { type: DataTypes.BIGINT.UNSIGNED, primaryKey: true, autoIncrement: true },
      examId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      userId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      paperQuestionId: { type: DataTypes.BIGINT.UNSIGNED, allowNull: false },
      givenAnswer: { type: DataTypes.JSON, allowNull: false },
      correctAnswer: { type: DataTypes.JSON, allowNull: false },
      scoreVersion: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
    },
    {
      tableName: 'wrong_questions',
      indexes: [
        { fields: ['user_id'] },
        // One record per question per attempt per score version; a review
        // may add corrected/changed rows without touching old ones.
        { unique: true, fields: ['exam_id', 'paper_question_id', 'score_version'] },
      ],
    }
  );
