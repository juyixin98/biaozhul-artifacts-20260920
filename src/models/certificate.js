const { DataTypes } = require('sequelize');

// Certificate of competency. At most one VALID certificate per exam
// (unique key on exam_id where revoked — implemented as a unique index on
// exam_id, because once a certificate is revoked the exam can never be
// re-issued: re-taking requires a new exam).
module.exports = (sequelize) =>
  sequelize.define(
    'Certificate',
    {
      id: { type: DataTypes.BIGINT.UNSIGNED, primaryKey: true, autoIncrement: true },
      certNo: { type: DataTypes.STRING(40), allowNull: false },
      examId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      userId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      paperId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      score: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false },
      masteryAtIssue: { type: DataTypes.TINYINT.UNSIGNED, allowNull: false },
      issuedAt: { type: DataTypes.DATE(3), allowNull: false },
      validFrom: { type: DataTypes.DATE(3), allowNull: false },
      validUntil: { type: DataTypes.DATE(3), allowNull: false },
      // valid | expired (12 months elapsed) | revoked (review lowered score)
      status: {
        type: DataTypes.ENUM('valid', 'expired', 'revoked'),
        allowNull: false,
        defaultValue: 'valid',
      },
      revokeReason: { type: DataTypes.STRING(500), allowNull: true },
    },
    {
      tableName: 'certificates',
      indexes: [
        // No duplicate certificate for the same exam, ever.
        { unique: true, fields: ['exam_id'] },
        { fields: ['user_id', 'status'] },
        { fields: ['cert_no'], unique: true },
      ],
    }
  );
