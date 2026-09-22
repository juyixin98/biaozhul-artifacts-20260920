'use strict';

// 全局配置：所有数值规则集中在此，便于测试与审计
module.exports = {
  port: Number(process.env.PORT || 3000),
  db: {
    host: process.env.DB_HOST || '127.0.0.1',
    port: Number(process.env.DB_PORT || 3306),
    name: process.env.DB_NAME || 'careops',
    user: process.env.DB_USER || 'root',
    password: process.env.DB_PASSWORD || 'careops_pw',
  },
  exam: {
    durationMinutes: Number(process.env.EXAM_DURATION_MINUTES || 30), // 考试限时 30 分钟
    dailyStartLimit: Number(process.env.DAILY_START_LIMIT || 3), // 每组织日最多开始次数
    questionCount: 20, // 每份试卷题量
    hardMinRatio: 0.3, // 难度不低于 4 的题最少占比 30%
    hardMinDifficulty: 4,
    masteryThreshold: Number(process.env.CERT_MASTERY_MIN || 85), // 发证掌握度门槛
    scoreThreshold: Number(process.env.CERT_SCORE_MIN || 80), // 发证当次分数门槛
    certValidMonths: 12, // 证书有效期 12 个月
    decayAfterDays: 30, // 30 天无学习
    decayPoints: 5, // 下降 5 分
  },
};
