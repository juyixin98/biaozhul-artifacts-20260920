'use strict';

const { sequelize } = require('./db');
const config = require('./config');
const { createApp } = require('./app');
const examService = require('./services/examService');
const clock = require('./config/clock');

async function main() {
  await sequelize.authenticate();
  const app = createApp();

  // 每分钟扫描超时场次，把它们推进到唯一终态（0 分；已交卷者不受影响）
  const sweep = setInterval(() => {
    examService.finalizeExpired().catch(() => {});
  }, 60000);
  sweep.unref?.();

  app.listen(config.port, () => {
    console.log(`CareOps 考核服务已启动: http://localhost:${config.port} (时钟${clock.isFrozen() ? '已冻结' : '系统时间'})`);
  });
}

main().catch((err) => {
  console.error('启动失败：', err);
  process.exit(1);
});
