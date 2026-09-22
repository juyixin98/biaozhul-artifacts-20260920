const {
  sequelize,
  Exam,
  ScoreVersion,
  Organization,
} = require('../models');
const config = require('../config');
const { dayKeyInTimezone, wholePeriodsElapsed } = require('../utils/time');

// Mastery = most recent VALID exam percentage score.
// A "valid exam" is one the student actually submitted (status 'submitted')
// with a current effective score (the highest score-version row).
//
// Decay rule (boundaries made explicit):
//   - Each continuous inactive block of 30 days subtracts 5 points.
//   - "Inactive" = no new valid submitted exam between the last activity
//     instant and `now`.
//   - Block boundary is inclusive: at exactly 30*24h the first -5 applies;
//     day 29 no decay; day 59 still -5; day 60 -> -10. Floor is 0.
//   - The counter resets with every new valid submitted exam.
//   - Expired (never submitted) exams do NOT count as learning activity.
async function computeMastery(userId, now, options = {}) {
  const org =
    options.organization ||
    (await Organization.findByPk(options.organizationId)) ||
    null;
  const tz = (org && org.timezone) || 'UTC';

  const latest = await ScoreVersion.findOne({
    include: [
      {
        model: Exam,
        where: { userId, status: 'submitted' },
        required: true,
      },
    ],
    order: [
      [Exam, 'submitted_at', 'DESC'],
      ['version', 'DESC'],
    ],
    lock: options.transaction ? transactionLock(options.transaction) : undefined,
    transaction: options.transaction || undefined,
  });

  const dayKey = dayKeyInTimezone(now, tz);
  if (!latest) {
    return { mastery: 0, basis: 'no_valid_exam', dayKey, timezone: tz, lastActivityAt: null, decayApplied: 0 };
  }

  const lastActivity = latest.createdAt;
  const blocks = wholePeriodsElapsed(now, lastActivity, config.MASTERY_DECAY_DAYS);
  const decay = blocks * config.MASTERY_DECAY_POINTS;
  const mastery = Math.max(0, Math.min(100, latest.score - decay));

  return {
    mastery,
    basis: 'latest_valid_exam',
    dayKey,
    timezone: tz,
    lastActivityAt: lastActivity,
    baseScore: latest.score,
    decayApplied: decay,
    inactiveBlocks: blocks,
  };
}

function transactionLock(t) {
  return t.LOCK.UPDATE;
}

module.exports = { computeMastery };
