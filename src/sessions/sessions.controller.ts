import {
  Body,
  Controller,
  Delete,
  Get,
  Headers,
  Param,
  ParseIntPipe,
  Post,
  Query,
  UnauthorizedException,
} from '@nestjs/common';
import { CommandDto, CreateSessionDto, JoinSessionDto } from './dto';
import { SessionsService } from './sessions.service';
import { SnapshotService } from './snapshot.service';

@Controller('sessions')
export class SessionsController {
  constructor(
    private readonly service: SessionsService,
    private readonly snapshots: SnapshotService,
  ) {}

  @Post()
  createSession(@Body() dto: CreateSessionDto) {
    return this.service.createSession(dto);
  }

  @Post('join')
  join(@Body() dto: JoinSessionDto) {
    return this.service.join(dto);
  }

  @Post(':id/commands')
  executeCommand(
    @Param('id') id: string,
    @Headers('x-session-token') token: string,
    @Body() dto: CommandDto,
  ) {
    return this.service.executeCommand(id, token, dto);
  }

  @Get(':id/state')
  async getState(
    @Param('id') id: string,
    @Headers('x-session-token') token: string,
  ) {
    const actor = token
      ? await this.service.validateParticipant(id, token)
      : null;
    if (!actor) {
      // Reads must not leak state, and must not refresh activity either.
      throw new UnauthorizedException('invalid session token');
    }
    return this.snapshots.snapshot(id);
  }

  @Get(':id/events')
  getEvents(
    @Param('id') id: string,
    @Headers('x-session-token') token: string,
    @Query('after', ParseIntPipe) after: number,
  ) {
    return this.service.getEvents(id, token, after ?? 0);
  }

  @Post(':id/hand-raises')
  raiseHand(@Param('id') id: string, @Headers('x-session-token') token: string) {
    return this.service.raiseHand(id, token);
  }

  @Delete(':id/hand-raises/mine')
  cancelHand(@Param('id') id: string, @Headers('x-session-token') token: string) {
    return this.service.cancelHand(id, token);
  }

  @Post(':id/hand-raises/:participantId/resolve')
  resolveHand(
    @Param('id') id: string,
    @Param('participantId') participantId: string,
    @Headers('x-session-token') token: string,
  ) {
    return this.service.resolveHand(id, token, participantId);
  }
}
