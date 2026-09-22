import 'reflect-metadata';
import { Logger } from '@nestjs/common';
import { AppConfig } from './config/app-config';
import { createDataSource } from './db/data-source';
import { initializeDataSource } from './db/db.module';

// Standalone migration entry point: npm run migrate
async function run() {
  const config = new AppConfig();
  const ds = createDataSource(config);
  await initializeDataSource(ds, new Logger('Migrate'));
  await ds.destroy();
  // eslint-disable-next-line no-console
  console.log('Migrations complete');
}

run().catch((error) => {
  // eslint-disable-next-line no-console
  console.error(error);
  process.exit(1);
});
