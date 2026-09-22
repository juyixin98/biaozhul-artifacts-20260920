import { Module } from '@nestjs/common';
import { DbModule } from './db/db.module';
import { HealthController } from './health.controller';
import { PresentationsModule } from './presentations/presentations.module';
import { SessionsModule } from './sessions/sessions.module';

@Module({
  imports: [DbModule, PresentationsModule, SessionsModule],
  controllers: [HealthController],
})
export class AppModule {}
