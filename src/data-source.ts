import { DataSource, DataSourceOptions } from 'typeorm';

export function buildOrmConfig(): DataSourceOptions {
  const base: DataSourceOptions = {
    type: 'postgres',
    synchronize: false,
    logging: process.env.DB_LOGGING === 'true',
    entities: [__dirname + '/**/*.entity{.ts,.js}'],
    migrations: [__dirname + '/migrations/*{.ts,.js}'],
  };
  if (process.env.DATABASE_URL) {
    return { ...base, url: process.env.DATABASE_URL };
  }
  return {
    ...base,
    host: process.env.DB_HOST || 'localhost',
    port: Number(process.env.DB_PORT || 5432),
    username: process.env.DB_USER || 'postgres',
    password: process.env.DB_PASSWORD || 'postgres',
    database: process.env.DB_NAME || 'stagevault',
  };
}

// Used by the TypeORM CLI (migrations).
const dataSource = new DataSource(buildOrmConfig());
export default dataSource;
