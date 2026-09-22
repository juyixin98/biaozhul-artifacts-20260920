const {
  sequelize,
  Certificate,
  Exam,
  Paper,
} = require('../models');
const config = require('../config');
const { addMonths } = require('../utils/time');

function buildCertNo(certId, issuedAt) {
  const year = issuedAt.getUTCFullYear();
  return `CERT-${year}-${String(certId).padStart(8, '0')}`;
}

// Issue conditions (ALL must hold at issue instant):
//   mastery >= 85 AND the exam's current score >= 80.
// Valid for exactly 12 calendar months from issue.
// One certificate per exam, ever (unique index on exam_id) — retaking
// produces a new exam, never a second certificate for an old one.
async function issueForExamIfEligible(exam, { score, mastery, now, transaction }) {
  const existing = await Certificate.findOne({ where: { examId: exam.id }, transaction, lock: transaction.LOCK.UPDATE });
  if (existing) return existing; // never duplicate, never re-issue

  if (score < config.SCORE_CERT_THRESHOLD || mastery < config.MASTERY_CERT_THRESHOLD) {
    return null;
  }

  const validFrom = new Date(now.getTime());
  const validUntil = addMonths(validFrom, config.CERT_VALIDITY_MONTHS);
  const created = await Certificate.create(
    {
      certNo: 'pending',
      examId: exam.id,
      userId: exam.userId,
      paperId: exam.paperId,
      score,
      masteryAtIssue: mastery,
      issuedAt: validFrom,
      validFrom,
      validUntil,
      status: 'valid',
    },
    { transaction }
  );
  created.certNo = buildCertNo(created.id, validFrom);
  await created.save({ transaction });
  return created;
}

// Time-based status. Storage status is only 'valid' or 'revoked';
// 'expired' is derived at read time from the 12-month boundary.
function effectiveStatus(cert, now) {
  if (cert.status === 'revoked') return 'revoked';
  return now.getTime() >= cert.validUntil.getTime() ? 'expired' : 'valid';
}

function serialize(cert, now) {
  return {
    id: cert.id,
    certNo: cert.certNo,
    examId: cert.examId,
    userId: cert.userId,
    paperId: cert.paperId,
    score: cert.score,
    masteryAtIssue: cert.masteryAtIssue,
    issuedAt: cert.issuedAt.toISOString(),
    validFrom: cert.validFrom.toISOString(),
    validUntil: cert.validUntil.toISOString(),
    // Boundary rule: valid when now < validUntil; at the exact instant
    // validUntil is reached the certificate is expired.
    status: effectiveStatus(cert, now),
    revokeReason: cert.revokeReason || null,
  };
}

// Recompute a certificate after a review changes the exam score.
//   score stays >= 80 AND cert was revoked only by review-lowered-score
//     -> restore to valid
//   score falls below 80 -> revoke with reason
// Mastery at issue is NOT re-evaluated here: the issue-time mastery is a
// historical fact, and the 85-mastery gate applies to issuance.
async function revalidateForExam(examId, newScore, reason, { now, transaction }) {
  const cert = await Certificate.findOne({ where: { examId }, transaction, lock: transaction.LOCK.UPDATE });
  if (!cert) {
    // A score crossing above 80 during review does not retroactively issue
    // a certificate: issuance happens at grading time.
    return null;
  }

  if (newScore < config.SCORE_CERT_THRESHOLD) {
    cert.status = 'revoked';
    cert.revokeReason = `review lowered score to ${newScore}: ${reason || 'no reason recorded'}`;
  } else if (cert.status === 'revoked' && (cert.revokeReason || '').startsWith('review lowered score')) {
    cert.status = 'valid';
    cert.revokeReason = `restored after review score set to ${newScore}`;
  }
  await cert.save({ transaction });
  return cert;
}

async function listForUser(userId, now) {
  const certs = await Certificate.findAll({
    where: { userId },
    include: [{ model: Exam, attributes: ['id', 'paperId'] }],
    order: [['issuedAt', 'DESC']],
  });
  return certs.map((c) => serialize(c, now));
}

module.exports = { issueForExamIfEligible, revalidateForExam, effectiveStatus, serialize, listForUser, buildCertNo };
