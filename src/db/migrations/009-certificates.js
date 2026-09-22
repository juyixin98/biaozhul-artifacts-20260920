'use strict';

module.exports = {
  async up(queryInterface, Sequelize) {
    await queryInterface.createTable('certificates', {
      id: { type: Sequelize.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      cert_number: { type: Sequelize.STRING(64), allowNull: false, unique: true },
      org_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      user_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      session_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false, unique: true },
      score: { type: Sequelize.DECIMAL(5, 2), allowNull: false },
      mastery_at_issue: { type: Sequelize.DECIMAL(5, 2), allowNull: false },
      issued_at: { type: Sequelize.DATE(3), allowNull: false },
      valid_until: { type: Sequelize.DATE(3), allowNull: false },
      status: { type: Sequelize.ENUM('issued', 'revoked'), allowNull: false, defaultValue: 'issued' },
      revoked_reason: { type: Sequelize.STRING(255), allowNull: true },
      created_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
      updated_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
    }, { charset: 'utf8mb4', collate: 'utf8mb4_unicode_ci' });
    await queryInterface.addIndex('certificates', ['user_id']);
    await queryInterface.addIndex('certificates', ['org_id']);
  },
  async down(queryInterface) {
    await queryInterface.dropTable('certificates');
  },
};
