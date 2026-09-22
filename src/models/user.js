const { DataTypes } = require('sequelize');

module.exports = (sequelize) =>
  sequelize.define(
    'User',
    {
      id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      organizationId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: true },
      // Null organizationId marks a platform-wide administrator.
      name: { type: DataTypes.STRING(128), allowNull: false },
      role: { type: DataTypes.ENUM('student', 'supervisor', 'admin'), allowNull: false },
    },
    {
      tableName: 'users',
      indexes: [{ fields: ['organization_id'] }],
    }
  );
