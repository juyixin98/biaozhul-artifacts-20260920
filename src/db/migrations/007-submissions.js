'use strict';

module.exports = {
  async up(queryInterface, Sequelize) {
    await queryInterface.createTable('submissions', {
      id: { type: Sequelize.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      session_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      request_id: { type: Sequelize.STRING(64), allowNull: false, unique: true },
      answers_hash: { type: Sequelize.STRING(64), allowNull: false },
      answers: { type: Sequelize.JSON, allowNull: false },
      client_submitted_at: { type: Sequelize.DATE(3), allowNull: true },
      arrival: { type: Sequelize.ENUM('arrived', 'late'), allowNull: false },
      duplicate_of_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: true },
      created_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
      updated_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
    }, { charset: 'utf8mb4', collate: 'utf8mb4_unicode_ci' });
    await queryInterface.addIndex('submissions', ['session_id']);
  },
  async down(queryInterface) {
    await queryInterface.dropTable('submissions');
  },
};
