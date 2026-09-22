'use strict';

module.exports = {
  async up(queryInterface, Sequelize) {
    await queryInterface.createTable('score_versions', {
      id: { type: Sequelize.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      session_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      version: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      score: { type: Sequelize.DECIMAL(5, 2), allowNull: false },
      detail: { type: Sequelize.JSON, allowNull: false },
      source: { type: Sequelize.ENUM('submit', 'review'), allowNull: false },
      reason: { type: Sequelize.STRING(255), allowNull: true },
      reviewer_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: true },
      is_current: { type: Sequelize.BOOLEAN, allowNull: false, defaultValue: false },
      created_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
      updated_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
    }, { charset: 'utf8mb4', collate: 'utf8mb4_unicode_ci' });
    await queryInterface.addIndex('score_versions', ['session_id', 'version'], { unique: true });
  },
  async down(queryInterface) {
    await queryInterface.dropTable('score_versions');
  },
};
