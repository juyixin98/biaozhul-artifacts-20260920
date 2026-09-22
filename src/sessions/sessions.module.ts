import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { CommandReceipt } from '../entities/command-receipt.entity';
import { HandRaise } from '../entities/hand-raise.entity';
import { Participant } from '../entities/participant.entity';
import { PresentationVersion } from '../entities/presentation-version.entity';
import { SessionEvent } from '../entities/session-event.entity';
import { Session } from '../entities/session.entity';
import { SessionTimeoutService } from './session-timeout.service';
import { SessionsController } from './sessions.controller';
import { SessionsGateway } from './sessions.gateway';
import { SessionsService } from './sessions.service';
import { SnapshotService } from './snapshot.service';

@Module({
  imports: [
    TypeOrmModule.forFeature([
      Session,
      Participant,
      SessionEvent,
      CommandReceipt,
      HandRaise,
      PresentationVersion,
    ]),
  ],
  controllers: [SessionsController],
  providers: [
    SessionsService,
    SessionsGateway,
    SnapshotService,
    SessionTimeoutService,
  ],
  exports: [SessionsService, SnapshotService, SessionTimeoutService],
})
export class SessionsModule {}
