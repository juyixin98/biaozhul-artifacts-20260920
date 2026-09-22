import { Module } from '@nestjs/common';
import { ScheduleModule } from '@nestjs/schedule';
import { TypeOrmModule } from '@nestjs/typeorm';
import { buildOrmConfig } from './data-source';
import { PresentationsModule } from './presentations/presentations.module';
import { SessionsModule } from './sessions/sessions.module';

@Module({
  imports: [
    TypeOrmModule.forRoot(buildOrmConfig()),
    ScheduleModule.forRoot(),
    PresentationsModule,
    SessionsModule,
  ],
})
export class AppModule {}
