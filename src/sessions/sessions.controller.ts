import {
  Body,
  Controller,
  Get,
  Headers,
  Param,
  Post,
  Query,
  UnauthorizedException,
} from '@nestjs/common';
import { SessionsService } from './sessions.service';
import { CreateSessionDto, JoinSessionDto } from './dto/session-dto';
import { CommandDto } from './dto/command.dto';

@Controller()
export class SessionsController {
  constructor(private readonly sessions: SessionsService) {}

  @Post('sessions')
  create(@Body() dto: CreateSessionDto) {
    return this.sessions.create(dto);
  }

  @Post('sessions/:code/join')
  join(@Param('code') code: string, @Body() dto: JoinSessionDto) {
    return this.sessions.join(code, dto);
  }

  // All host/participant commands flow through this endpoint. The command
  // carries requestId (idempotency) and expectedVersion (optimistic
  // concurrency / event sequence). Identity comes from the header, never from
  // the body, so a client cannot impersonate another participant.
  @Post('sessions/:code/commands')
  command(
    @Param('code') code: string,
    @Body() dto: CommandDto,
    @Headers('x-user-id') actorId = '',
  ) {
    if (!actorId) throw new UnauthorizedException('x-user-id header is required');
    (dto as CommandDto & { actorId?: string }).actorId = actorId;
    return this.sessions.executeCommand(code, dto);
  }

  @Get('sessions/:code/snapshot')
  snapshot(@Param('code') code: string, @Headers('x-user-id') userId = '') {
    return this.sessions.snapshotByCode(code, userId);
  }

  @Get('sessions/:code/events')
  events(
    @Param('code') code: string,
    @Headers('x-user-id') userId = '',
    @Query('afterSeq') afterSeq = '-1',
  ) {
    const seq = Number.isFinite(Number(afterSeq)) ? Number(afterSeq) : -1;
    return this.sessions.eventsByCode(code, userId, seq);
  }
}
