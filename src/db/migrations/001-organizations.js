'use strict';

module.exports = {
  async up(queryInterface, Sequelize) {
    await queryInterface.createTable('organizations', {
      id: { type: Sequelize.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      name: { type: Sequelize.STRING(128), allowNull: false },
      timezone: { type: Sequelize.STRING(64), allowNull: false, defaultValue: 'UTC' },
      created_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
      updated_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
    }, { charset: 'utf8mb4', collate: 'utf8mb4_unicode_ci' });
  },
  async down(queryInterface) {
    await queryInterface.dropTable('organizations');
  },
};
