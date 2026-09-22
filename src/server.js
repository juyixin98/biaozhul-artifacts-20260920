const app = require('./app');
const sequelize = require('./db/connection');
const config = require('./config');

async function main() {
  await sequelize.authenticate();
  app.listen(config.port, () => {
    // eslint-disable-next-line no-console
    console.log(`CareOps assessment service listening on :${config.port}`);
  });
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error('failed to start service', err);
  process.exit(1);
});
