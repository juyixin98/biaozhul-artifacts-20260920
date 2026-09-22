'use strict';

module.exports = {
  async up(queryInterface, Sequelize) {
    await queryInterface.createTable('learning_activities', {
      id: { type: Sequelize.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      org_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      user_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      type: { type: Sequelize.STRING(32), allowNull: false, defaultValue: 'exam' },
      ref_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: true },
      occurred_at: { type: Sequelize.DATE(3), allowNull: false },
      note: { type: Sequelize.STRING(255), allowNull: true },
      created_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
      updated_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
    }, { charset: 'utf8mb4', collate: 'utf8mb4_unicode_ci' });
    await queryInterface.addIndex('learning_activities', ['user_id', 'occurred_at']);
  },
  async down(queryInterface) {
    await queryInterface.dropTable('learning_activities');
  },
};
