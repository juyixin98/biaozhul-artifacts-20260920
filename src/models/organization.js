const { DataTypes } = require('sequelize');

// Care organization. The organization timezone is the source of truth for
// "per day" attempt windows and certificate validity boundaries.
module.exports = (sequelize) =>
  sequelize.define(
    'Organization',
    {
      id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
      name: { type: DataTypes.STRING(128), allowNull: false },
      timezone: { type: DataTypes.STRING(64), allowNull: false, defaultValue: 'UTC' },
    },
    { tableName: 'organizations' }
  );
