import { Module } from '@nestjs/common';
import { SessionsController } from './sessions.controller';
import { SessionsService } from './sessions.service';
import { RealtimeGateway } from './realtime.gateway';
import { TimeoutScheduler } from './timeout.scheduler';

@Module({
  controllers: [SessionsController],
  providers: [SessionsService, RealtimeGateway, TimeoutScheduler],
  exports: [SessionsService, RealtimeGateway],
})
export class SessionsModule {}
