import { Global, Inject, Logger, Module, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import { Provider } from '@nestjs/common/interfaces';
import { DataSource } from 'typeorm';
import { AppConfig } from '../config/app-config';
import { createDataSource, DATA_SOURCE } from './data-source';

const dataSourceProvider: Provider = {
  provide: DATA_SOURCE,
  useFactory: (config: AppConfig) => createDataSource(config),
  inject: [AppConfig],
};

// Wait for Postgres, run pending migrations. Migrations make the durable state
// (including timeout deadlines and event log) ready before the app serves
// traffic, so timeout work survives restarts.
export async function initializeDataSource(
  ds: DataSource,
  logger: Logger,
  attempts = 30,
): Promise<void> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      if (!ds.isInitialized) await ds.initialize();
      break;
    } catch (error) {
      lastError = error;
      logger.warn(`Database not ready (attempt ${attempt}/${attempts}): ${String(error)}`);
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }
  if (!ds.isInitialized) throw lastError;

  const ran = await ds.runMigrations();
  if (ran.length > 0) logger.log(`Applied ${ran.length} migration(s)`);
}

@Global()
@Module({
  providers: [{ provide: AppConfig, useClass: AppConfig }, dataSourceProvider],
  exports: [AppConfig, DATA_SOURCE],
})
export class DbModule implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(DbModule.name);

  constructor(@Inject(DATA_SOURCE) private readonly ds: DataSource) {}

  async onModuleInit(): Promise<void> {
    await initializeDataSource(this.ds, this.logger);
  }

  async onModuleDestroy(): Promise<void> {
    if (this.ds.isInitialized) await this.ds.destroy();
  }
}
