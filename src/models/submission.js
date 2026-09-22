const { DataTypes } = require('sequelize');

// A submission records the client-asserted content fingerprint for a given
// client request ID. The request ID is the idempotency key:
//   - same id + same content      -> return the original grade (never regrade)
//   - same id + different content -> 409 Conflict
// contentHash is taken over the canonical JSON encoding of the answers.
module.exports = (sequelize) =>
  sequelize.define(
    'Submission',
    {
      id: { type: DataTypes.BIGINT.UNSIGNED, primaryKey: true, autoIncrement: true },
      examId: { type: DataTypes.INTEGER.UNSIGNED, allowNull: false },
      // Client-supplied idempotency key (required).
      requestId: { type: DataTypes.STRING(128), allowNull: false },
      contentHash: { type: DataTypes.CHAR(64), allowNull: false }, // sha256 hex
      answers: { type: DataTypes.JSON, allowNull: false },
      scoreVersionId: { type: DataTypes.BIGINT.UNSIGNED, allowNull: false },
      createdAt: { type: DataTypes.DATE(3), allowNull: false },
    },
    {
      tableName: 'submissions',
      updatedAt: false,
      indexes: [
        // One submission per request id per exam; this is the idempotency
        // guarantee and also serializes concurrent submit retries.
        { unique: true, fields: ['exam_id', 'request_id'] },
        { fields: ['score_version_id'] },
      ],
    }
  );
