'use strict';

const path = require('path');
const { Umzug, SequelizeStorage } = require('umzug');
const { sequelize, Sequelize } = require('./index');

// Umzug 迁移运行器：迁移文件为标准 up(queryInterface, Sequelize)/down 形态
const umzug = new Umzug({
  migrations: {
    glob: ['migrations/*.js', { cwd: __dirname }],
    resolve: ({ name, path: migrationPath, context }) => {
      // eslint-disable-next-line global-require, import/no-dynamic-require
      const migration = require(path.resolve(migrationPath));
      return {
        name,
        up: async () => migration.up(context, Sequelize),
        down: async () => migration.down(context, Sequelize),
      };
    },
  },
  context: sequelize.getQueryInterface(),
  storage: new SequelizeStorage({ sequelize, tableName: 'migrations_meta' }),
  logger: undefined,
});

async function main() {
  const cmd = process.argv[2] || 'up';
  await sequelize.authenticate();
  if (cmd === 'up') {
    const ran = await umzug.up();
    console.log(`迁移完成：${ran.length ? ran.map((m) => m.name).join(', ') : '无新增'}`);
  } else if (cmd === 'down') {
    const reverted = await umzug.down();
    console.log(`回滚：${reverted.map((m) => m.name).join(', ') || '无'}`);
  } else if (cmd === 'status') {
    const executed = await umzug.executed();
    const pending = await umzug.pending();
    console.log(`已执行：${executed.map((m) => m.name).join(', ') || '无'}`);
    console.log(`待执行：${pending.map((m) => m.name).join(', ') || '无'}`);
  }
  await sequelize.close();
}

if (require.main === module) {
  main().catch((err) => {
    console.error('迁移失败：', err);
    process.exit(1);
  });
}

module.exports = umzug;
