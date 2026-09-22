'use strict';

module.exports = {
  async up(queryInterface, Sequelize) {
    await queryInterface.createTable('papers', {
      id: { type: Sequelize.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      org_id: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      seed: { type: Sequelize.STRING(64), allowNull: false },
      title: { type: Sequelize.STRING(128), allowNull: false },
      question_count: { type: Sequelize.INTEGER.UNSIGNED, allowNull: false },
      scoring_rule: { type: Sequelize.JSON, allowNull: false },
      created_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
      updated_at: { type: Sequelize.DATE(3), allowNull: false, defaultValue: Sequelize.literal('CURRENT_TIMESTAMP(3)') },
    }, { charset: 'utf8mb4', collate: 'utf8mb4_unicode_ci' });
    await queryInterface.addIndex('papers', ['org_id', 'seed'], { unique: true });
  },
  async down(queryInterface) {
    await queryInterface.dropTable('papers');
  },
};
