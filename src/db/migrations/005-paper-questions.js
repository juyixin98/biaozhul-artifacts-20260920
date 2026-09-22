'use strict';

module.exports = {
  async up(queryInterface, Sequelize) {
    await queryInterface.createTable('paper_questions', {
      id: { type: Sequelize.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      paper_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      question_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      order: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      type: { type: Sequelize.ENUM('single', 'multiple', 'judge'), allowNull: false },
      difficulty: { type: Sequelize.TINYINT.UNSIGNED, allowNull: false },
      stem: { type: Sequelize.TEXT, allowNull: false },
      options: { type: Sequelize.JSON, allowNull: true },
      answer: { type: Sequelize.JSON, allowNull: false },
      explanation: { type: Sequelize.TEXT, allowNull: true },
      points: { type: Sequelize.DECIMAL(5, 2), allowNull: false },
      created_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
      updated_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
    }, { charset: 'utf8mb4', collate: 'utf8mb4_unicode_ci' });
    await queryInterface.addIndex('paper_questions', ['paper_id', 'order'], { unique: true });
    await queryInterface.addIndex('paper_questions', ['question_id']);
  },
  async down(queryInterface) {
    await queryInterface.dropTable('paper_questions');
  },
};
