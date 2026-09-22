'use strict';

module.exports = {
  async up(queryInterface, Sequelize) {
    await queryInterface.createTable('exam_sessions', {
      id: { type: Sequelize.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      org_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      paper_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      user_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      status: { type: Sequelize.ENUM('in_progress', 'graded'), allowNull: false, defaultValue: 'in_progress' },
      started_at: { type: Sequelize.DATE(3), allowNull: false },
      deadline_at: { type: Sequelize.DATE(3), allowNull: false },
      submitted_at: { type: Sequelize.DATE(3), allowNull: true },
      finalized_at: { type: Sequelize.DATE(3), allowNull: true },
      final_reason: { type: Sequelize.ENUM('submit', 'timeout'), allowNull: true },
      current_score_version_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: true },
      created_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
      updated_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
    }, { charset: 'utf8mb4', collate: 'utf8mb4_unicode_ci' });
    await queryInterface.addIndex('exam_sessions', ['user_id', 'started_at']);
    await queryInterface.addIndex('exam_sessions', ['org_id', 'status']);
  },
  async down(queryInterface) {
    await queryInterface.dropTable('exam_sessions');
  },
};
