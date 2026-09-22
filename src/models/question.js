const { DataTypes } = require('sequelize');

// Live question bank. Historical papers reference frozen SNAPSHOT copies in
// paper_questions, so editing or deleting rows here never alters a paper.
module.exports = (sequelize) =>
  sequelize.define(
    'Question',
    {
      id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      organizationId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: true },
      // Null organizationId = question shared across all organizations.
      type: {
        type: DataTypes.ENUM('single', 'multiple', 'boolean'),
        allowNull: false,
      },
      difficulty: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false }, // 1..5
      stem: { type: DataTypes.TEXT, allowNull: false },
      // ["A","B","C","D"]; booleans have ["true","false"].
      options: { type: DataTypes.JSON, allowNull: false },
      // Canonical sorted array, e.g. ["A"] / ["A","C"] / ["true"].
      answer: { type: DataTypes.JSON, allowNull: false },
      explanation: { type: DataTypes.TEXT, allowNull: false, defaultValue: '' },
      active: { type: DataTypes.BOOLEAN, allowNull: false, defaultValue: true },
    },
    {
      tableName: 'questions',
      indexes: [
        { fields: ['organization_id'] },
        { fields: ['active'] },
        { fields: ['difficulty'] },
      ],
    }
  );
