'use strict';

module.exports = {
  async up(queryInterface, Sequelize) {
    await queryInterface.createTable('questions', {
      id: { type: Sequelize.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      org_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      type: { type: Sequelize.ENUM('single', 'multiple', 'judge'), allowNull: false },
      difficulty: { type: Sequelize.TINYINT.UNSIGNED, allowNull: false },
      stem: { type: Sequelize.TEXT, allowNull: false },
      options: { type: Sequelize.JSON, allowNull: true },
      answer: { type: Sequelize.JSON, allowNull: false },
      explanation: { type: Sequelize.TEXT, allowNull: true },
      points: { type: Sequelize.DECIMAL(5, 2), allowNull: false, defaultValue: 5 },
      active: { type: Sequelize.BOOLEAN, allowNull: false, defaultValue: true },
      created_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
      updated_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
    }, { charset: 'utf8mb4', collate: 'utf8mb4_unicode_ci' });
    await queryInterface.addIndex('questions', ['org_id', 'difficulty']);
  },
  async down(queryInterface) {
    await queryInterface.dropTable('questions');
  },
};
