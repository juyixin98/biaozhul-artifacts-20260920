'use strict';

// 组织：携带 IANA 时区，每日开考次数按该时区的自然日计算
module.exports = (sequelize, DataTypes) =>
  sequelize.define('Organization', {
    id: { type: DataTypes.INTEGER.UNSIGNED, primaryKey: true, autoIncrement: true },
    name: { type: DataTypes.STRING(128), allowNull: false },
    timezone: { type: DataTypes.STRING(64), allowNull: false, defaultValue: 'UTC' },
  }, { tableName: 'organizations', underscored: true });
