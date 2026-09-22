const { DataTypes } = require('sequelize');

// Count of started attempts per user per organization-local calendar day.
// Keyed by date string computed in the organization's timezone so that
// "3 starts per day" follows the organization's midnight boundary.
module.exports = (sequelize) =>
  sequelize.define(
    'DailyAttemptCount',
    {
      id: { type: DataTypes.BIGINT.UNSIGNED, primaryKey: true, autoIncrement: true },
      userId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      dayKey: { type: DataTypes.CHAR(10), allowNull: false }, // YYYY-MM-DD in org tz
      count: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false, defaultValue: 0 },
    },
    {
      tableName: 'daily_attempt_counts',
      timestamps: false,
      indexes: [{ unique: true, fields: ['user_id', 'day_key'] }],
    }
  );
