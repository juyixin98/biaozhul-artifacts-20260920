import 'reflect-metadata';
import { ValidationPipe, Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module';
import { AppConfig } from './config/app-config';

async function bootstrap() {
  const app = await NestFactory.create(AppModule, { cors: true });
  app.useGlobalPipes(
    new ValidationPipe({
      whitelist: true,
      transform: true,
      forbidNonWhitelisted: false,
    }),
  );
  const config = app.get(AppConfig);
  await app.listen(config.port, '0.0.0.0');
  new Logger('Bootstrap').log(`StageVault listening on :${config.port}`);
}
bootstrap().catch((error) => {
  // eslint-disable-next-line no-console
  console.error('Failed to start', error);
  process.exit(1);
});
