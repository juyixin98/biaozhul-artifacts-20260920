import {
  BadRequestException,
  ConflictException,
  ForbiddenException,
  Injectable,
  Logger,
  NotFoundException,
  UnauthorizedException,
} from '@nestjs/common';
import { InjectDataSource, InjectRepository } from '@nestjs/typeorm';
import {
  DataSource,
  EntityManager,
  MoreThan,
  QueryFailedError,
  Repository,
} from 'typeorm';
import { CommandReceipt } from '../entities/command-receipt.entity';
import { HandRaise } from '../entities/hand-raise.entity';
import { Participant } from '../entities/participant.entity';
import { PresentationVersion } from '../entities/presentation-version.entity';
import { SessionEvent } from '../entities/session-event.entity';
import { MAX_PARTICIPANTS, Session } from '../entities/session.entity';
import { CommandDto, CreateSessionDto, JoinSessionDto } from './dto';
import { SessionsGateway } from './sessions.gateway';
import { generateJoinCode, generateToken } from './tokens';

export interface CommandResult {
  requestId: string;
  version: number;
  eventSeq: number;
  status: string;
  currentSceneIndex: number;
  replayed: boolean;
}

@Injectable()
export class SessionsService {
  private readonly logger = new Logger(SessionsService.name);

  constructor(
    @InjectDataSource() private readonly dataSource: DataSource,
    @InjectRepository(CommandReceipt)
    private readonly receipts: Repository<CommandReceipt>,
    private readonly gateway: SessionsGateway,
  ) {}

  // ---------------------------------------------------------------- sessions

  async createSession(dto: CreateSessionDto) {
    const version = await this.dataSource
      .getRepository(PresentationVersion)
      .findOne({ where: { id: dto.presentationVersionId } });
    if (!version) throw new NotFoundException('presentation version not found');
    if (!version.published) {
      throw new BadRequestException(
        'sessions can only bind to a published version',
      );
    }
    // Retry a few times in the unlikely case of a join-code collision.
    for (let attempt = 0; attempt < 5; attempt++) {
      try {
        return await this.dataSource.transaction(async (manager) => {
          const session = await manager.save(
            Session,
            manager.create(Session, {
              joinCode: generateJoinCode(),
              presentationVersionId: version.id,
              status: 'lobby',
              currentSceneIndex: 0,
              version: 0,
              eventSeq: 0,
              lastActivityAt: new Date(),
            }),
          );
          const host = await manager.save(
            Participant,
            manager.create(Participant, {
              sessionId: session.id,
              name: dto.hostName,
              role: 'host',
              token: generateToken(),
            }),
          );
          return {
            sessionId: session.id,
            joinCode: session.joinCode,
            hostParticipantId: host.id,
            hostToken: host.token,
          };
        });
      } catch (e) {
        if (!isUniqueViolation(e)) throw e;
      }
    }
    throw new ConflictException('could not allocate a unique join code');
  }

  /**
   * Join by 6-char code. The session row is locked FOR UPDATE inside the
   * transaction, so concurrent joins are serialized and the 25-participant
   * cap can never be exceeded.
   */
  async join(dto: JoinSessionDto) {
    return this.dataSource.transaction(async (manager) => {
      const session = await this.lockSessionByCode(manager, dto.joinCode);
      if (!session) throw new NotFoundException('invalid join code');
      if (session.status === 'ended') {
        throw new ConflictException({
          code: 'SESSION_ENDED',
          message: 'session has ended',
        });
      }
      const count = await manager.getRepository(Participant).count({
        where: { sessionId: session.id },
      });
      if (count >= MAX_PARTICIPANTS) {
        throw new ConflictException({
          code: 'SESSION_FULL',
          message: `session is full (max ${MAX_PARTICIPANTS} participants)`,
        });
      }
      const participant = await manager.save(
        Participant,
        manager.create(Participant, {
          sessionId: session.id,
          name: dto.name,
          role: 'participant',
          token: generateToken(),
        }),
      );
      // A successful join is valid activity.
      session.lastActivityAt = new Date();
      await manager.save(Session, session);
      return {
        sessionId: session.id,
        participantId: participant.id,
        token: participant.token,
        role: participant.role,
        eventSeq: session.eventSeq,
      };
    });
  }

  // ---------------------------------------------------------------- commands

  /**
   * Host command with idempotency + optimistic concurrency.
   *
   * - Same requestId with the same content returns the stored result.
   * - Same requestId with different content is rejected (409).
   * - expectedVersion must match the current version, else 409.
   * - Ended sessions reject every command; end is terminal.
   *
   * The state change, version bump, event insert and receipt insert all
   * happen in ONE transaction; the WebSocket broadcast happens only after
   * commit, so a crash before broadcast never loses the event (clients
   * replay it from the session_events table on reconnect).
   */
  async executeCommand(
    sessionId: string,
    token: string | undefined,
    dto: CommandDto,
  ): Promise<CommandResult> {
    if (!token) throw new UnauthorizedException('missing session token');
    const requestHash = hashCommand(dto);

    // Fast path: idempotent replay of an already-applied command.
    const existing = await this.receipts.findOne({
      where: { sessionId, requestId: dto.requestId },
    });
    if (existing) {
      if (existing.requestHash !== requestHash) {
        throw new ConflictException({
          code: 'REQUEST_ID_REUSED',
          message: 'requestId was already used with different content',
        });
      }
      return { ...(existing.result as object), replayed: true } as CommandResult;
    }

    let event: SessionEvent | null = null;
    let result: CommandResult;
    try {
      result = await this.dataSource.transaction(async (manager) => {
        const session = await this.lockSession(manager, sessionId);
        if (!session) throw new NotFoundException('session not found');
        // Re-check under the row lock: a concurrent transaction with the
        // same requestId may have committed while we waited for the lock.
        const committed = await manager.getRepository(CommandReceipt).findOne({
          where: { sessionId, requestId: dto.requestId },
        });
        if (committed) {
          if (committed.requestHash !== requestHash) {
            throw new ConflictException({
              code: 'REQUEST_ID_REUSED',
              message: 'requestId was already used with different content',
            });
          }
          return { ...(committed.result as object), replayed: true } as CommandResult;
        }
        const actor = await this.requireActor(manager, sessionId, token);
        if (actor.role !== 'host') {
          throw new ForbiddenException('only the host can send commands');
        }
        if (session.status === 'ended') {
          throw new ConflictException({
            code: 'SESSION_ENDED',
            message: 'session has ended and cannot be resumed',
          });
        }
        if (session.version !== dto.expectedVersion) {
          throw new ConflictException({
            code: 'VERSION_CONFLICT',
            message: `expected version ${dto.expectedVersion}, current is ${session.version}`,
            currentVersion: session.version,
          });
        }

        const versionRow = await manager
          .getRepository(PresentationVersion)
          .findOne({ where: { id: session.presentationVersionId } });
        this.applyCommand(session, dto, versionRow?.scenes?.length ?? 0);

        session.version += 1;
        session.lastActivityAt = new Date();
        event = await this.appendEvent(manager, session, dto.type, {
          command: dto.type,
          sceneIndex: session.currentSceneIndex,
          status: session.status,
          by: actor.id,
          version: session.version,
        });
        await manager.save(Session, session);

        const commandResult: CommandResult = {
          requestId: dto.requestId,
          version: session.version,
          eventSeq: session.eventSeq,
          status: session.status,
          currentSceneIndex: session.currentSceneIndex,
          replayed: false,
        };
        await manager.save(
          CommandReceipt,
          manager.create(CommandReceipt, {
            sessionId,
            requestId: dto.requestId,
            commandType: dto.type,
            requestHash,
            result: { ...commandResult, replayed: false } as never,
          }),
        );
        return commandResult;
      });
    } catch (e) {
      // Concurrent duplicate requestId: the other transaction won; return
      // its stored result instead of failing.
      if (isUniqueViolation(e)) {
        const receipt = await this.receipts.findOne({
          where: { sessionId, requestId: dto.requestId },
        });
        if (receipt && receipt.requestHash === requestHash) {
          return { ...(receipt.result as object), replayed: true } as CommandResult;
        }
      }
      throw e;
    }

    this.broadcast(sessionId, event);
    return result;
  }

  private applyCommand(
    session: Session,
    dto: CommandDto,
    sceneCount: number,
  ): void {
    const requireStatus = (allowed: string[]) => {
      if (!allowed.includes(session.status)) {
        throw new ConflictException({
          code: 'INVALID_TRANSITION',
          message: `cannot ${dto.type} while session is ${session.status}`,
        });
      }
    };
    switch (dto.type) {
      case 'start':
        requireStatus(['lobby']);
        session.status = 'live';
        break;
      case 'pause':
        requireStatus(['live']);
        session.status = 'paused';
        break;
      case 'resume':
        requireStatus(['paused']);
        session.status = 'live';
        break;
      case 'goto_scene':
        requireStatus(['live', 'paused']);
        if (
          dto.sceneIndex === undefined ||
          dto.sceneIndex < 0 ||
          dto.sceneIndex >= sceneCount
        ) {
          throw new BadRequestException(
            `sceneIndex must be between 0 and ${sceneCount - 1}`,
          );
        }
        session.currentSceneIndex = dto.sceneIndex;
        break;
      case 'end':
        requireStatus(['lobby', 'live', 'paused']);
        session.status = 'ended';
        session.endedAt = new Date();
        break;
      default:
        throw new BadRequestException(`unknown command type ${dto.type}`);
    }
  }

  // ------------------------------------------------------------- hand raises

  /**
   * Raise hand. Queue order is the server receive order (identity PK).
   * A duplicate raise while one is already active returns the existing row
   * instead of enqueueing twice (enforced by a partial unique index).
   */
  async raiseHand(sessionId: string, token: string | undefined) {
    if (!token) throw new UnauthorizedException('missing session token');
    try {
      const { handRaise, event, duplicate } = await this.dataSource.transaction(
        async (manager) => {
          const session = await this.lockSession(manager, sessionId);
          if (!session) throw new NotFoundException('session not found');
          const me = await this.requireActor(manager, sessionId, token);
          if (session.status === 'ended') {
            throw new ConflictException({
              code: 'SESSION_ENDED',
              message: 'session has ended',
            });
          }
          const existing = await manager.getRepository(HandRaise).findOne({
            where: { sessionId, participantId: me.id, status: 'raised' },
          });
          if (existing) {
            return { handRaise: existing, event: null, duplicate: true };
          }
          const handRaise = await manager.save(
            HandRaise,
            manager.create(HandRaise, {
              sessionId,
              participantId: me.id,
              status: 'raised',
            }),
          );
          session.lastActivityAt = new Date();
          const event = await this.appendEvent(manager, session, 'hand_raised', {
            handRaiseId: handRaise.id,
            participantId: me.id,
            name: me.name,
          });
          await manager.save(Session, session);
          return { handRaise, event, duplicate: false };
        },
      );
      if (event) this.broadcast(sessionId, event);
      return { handRaiseId: handRaise.id, duplicate };
    } catch (e) {
      // Lost a race against our own concurrent duplicate request.
      if (isUniqueViolation(e)) {
        const me = await this.dataSource
          .getRepository(Participant)
          .findOne({ where: { sessionId, token } });
        const existing = await this.dataSource
          .getRepository(HandRaise)
          .findOne({
            where: { sessionId, participantId: me?.id, status: 'raised' },
          });
        if (existing) return { handRaiseId: existing.id, duplicate: true };
      }
      throw e;
    }
  }

  /** Participants can only cancel their own raised hand. */
  async cancelHand(sessionId: string, token: string | undefined) {
    if (!token) throw new UnauthorizedException('missing session token');
    const { event, handRaise } = await this.dataSource.transaction(
      async (manager) => {
        const session = await this.lockSession(manager, sessionId);
        if (!session) throw new NotFoundException('session not found');
        const me = await this.requireActor(manager, sessionId, token);
        const handRaise = await manager.getRepository(HandRaise).findOne({
          where: { sessionId, participantId: me.id, status: 'raised' },
        });
        if (!handRaise) {
          throw new NotFoundException('no active raised hand for you');
        }
        handRaise.status = 'cancelled';
        handRaise.handledAt = new Date();
        await manager.save(HandRaise, handRaise);
        session.lastActivityAt = new Date();
        const event = await this.appendEvent(manager, session, 'hand_cancelled', {
          handRaiseId: handRaise.id,
          participantId: me.id,
        });
        await manager.save(Session, session);
        return { event, handRaise };
      },
    );
    this.broadcast(sessionId, event);
    return { handRaiseId: handRaise.id, status: 'cancelled' };
  }

  /** Host acknowledges a raised hand, removing it from the queue. */
  async resolveHand(
    sessionId: string,
    token: string | undefined,
    participantId: string,
  ) {
    if (!token) throw new UnauthorizedException('missing session token');
    const { event, handRaise } = await this.dataSource.transaction(
      async (manager) => {
        const session = await this.lockSession(manager, sessionId);
        if (!session) throw new NotFoundException('session not found');
        const actor = await this.requireActor(manager, sessionId, token);
        if (actor.role !== 'host') {
          throw new ForbiddenException('only the host can resolve hands');
        }
        const handRaise = await manager.getRepository(HandRaise).findOne({
          where: { sessionId, participantId, status: 'raised' },
        });
        if (!handRaise) {
          throw new NotFoundException('no active raised hand for participant');
        }
        handRaise.status = 'resolved';
        handRaise.handledAt = new Date();
        await manager.save(HandRaise, handRaise);
        session.lastActivityAt = new Date();
        const event = await this.appendEvent(manager, session, 'hand_resolved', {
          handRaiseId: handRaise.id,
          participantId,
        });
        await manager.save(Session, session);
        return { event, handRaise };
      },
    );
    this.broadcast(sessionId, event);
    return { handRaiseId: handRaise.id, status: 'resolved' };
  }

  // ------------------------------------------------------------------ reads

  async getEvents(sessionId: string, token: string | undefined, after: number) {
    const actor = token
      ? await this.dataSource
          .getRepository(Participant)
          .findOne({ where: { sessionId, token } })
      : null;
    if (!actor) throw new UnauthorizedException('invalid session token');
    return this.dataSource.getRepository(SessionEvent).find({
      where: { sessionId, seq: MoreThan(after) },
      order: { seq: 'ASC' },
    });
  }

  async validateParticipant(sessionId: string, token: string) {
    return this.dataSource
      .getRepository(Participant)
      .findOne({ where: { sessionId, token } });
  }

  // ----------------------------------------------------------------- helpers

  /** Shared with the timeout sweeper: bump seq and insert the event row. */
  async appendEvent(
    manager: EntityManager,
    session: Session,
    type: string,
    payload: Record<string, unknown>,
  ): Promise<SessionEvent> {
    session.eventSeq += 1;
    return manager.save(
      SessionEvent,
      manager.create(SessionEvent, {
        sessionId: session.id,
        seq: session.eventSeq,
        type,
        payload: { ...payload, eventSeq: session.eventSeq },
      }),
    );
  }

  private lockSession(manager: EntityManager, sessionId: string) {
    return manager
      .getRepository(Session)
      .createQueryBuilder('s')
      .setLock('pessimistic_write')
      .where('s.id = :id', { id: sessionId })
      .getOne();
  }

  private lockSessionByCode(manager: EntityManager, joinCode: string) {
    return manager
      .getRepository(Session)
      .createQueryBuilder('s')
      .setLock('pessimistic_write')
      .where('s.join_code = :code', { code: joinCode })
      .getOne();
  }

  private async requireActor(
    manager: EntityManager,
    sessionId: string,
    token: string,
  ): Promise<Participant> {
    const actor = await manager
      .getRepository(Participant)
      .findOne({ where: { sessionId, token } });
    if (!actor) throw new UnauthorizedException('invalid session token');
    return actor;
  }

  /**
   * Broadcast after commit. A broadcast failure must never fail the command:
   * the event is durable and clients will replay it on reconnect.
   */
  private broadcast(sessionId: string, event: SessionEvent | null) {
    if (!event) return;
    try {
      this.gateway.broadcast(sessionId, event);
    } catch (e) {
      this.logger.warn(`broadcast failed for session ${sessionId}: ${e}`);
    }
  }
}

function isUniqueViolation(e: unknown): boolean {
  return e instanceof QueryFailedError && (e as any).code === '23505';
}

function hashCommand(dto: CommandDto): string {
  return JSON.stringify({ type: dto.type, sceneIndex: dto.sceneIndex ?? null });
}
