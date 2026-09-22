const env = process.env;

module.exports = {
  port: parseInt(env.PORT || '3000', 10),
  db: {
    host: env.DB_HOST || '127.0.0.1',
    port: parseInt(env.DB_PORT || '3306', 10),
    name: env.DB_NAME || 'careops',
    user: env.DB_USER || 'root',
    password: env.DB_PASSWORD || 'careops',
  },
  allowClockOverride: env.ALLOW_CLOCK_OVERRIDE === '1',
  enableDemoSeed: env.ENABLE_DEMO_SEED === 'true',
  // Exam rule constants — single source of truth.
  EXAM_DURATION_MINUTES: 30,
  DAILY_ATTEMPT_LIMIT: 3,
  PAPER_QUESTION_COUNT: 20,
  PAPER_HARD_RATIO: 0.3, // >= 30% of questions must have difficulty >= 4
  POINTS_PER_QUESTION: 5, // 20 questions * 5 points = 100
  MASTERY_DECAY_DAYS: 30,
  MASTERY_DECAY_POINTS: 5,
  MASTERY_CERT_THRESHOLD: 85,
  SCORE_CERT_THRESHOLD: 80,
  CERT_VALIDITY_MONTHS: 12,
};
