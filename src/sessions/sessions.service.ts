import { Inject, Injectable, Logger, forwardRef } from '@nestjs/common';
import { DataSource, EntityManager } from 'typeorm';
import { randomUUID } from 'crypto';
import { DATA_SOURCE } from '../db/data-source';
import { AppConfig } from '../config/app-config';
import { PresentationVersion } from '../presentations/presentation-version.entity';
import { Session, SessionStatus } from './session.entity';
import { Participant, ROLE_HOST, ROLE_PARTICIPANT } from './participant.entity';
import { HandRaise } from './hand-raise.entity';
import { SessionEvent } from './session-event.entity';
import { CommandRecord } from './command-record.entity';
import { CreateSessionDto, JoinSessionDto } from './dto/session-dto';
import { CommandDto } from './dto/command.dto';
import { Errors } from './session-errors';
import { generateJoinCode, hashBody } from './session-codes';
import { RealtimeGateway } from './realtime.gateway';

export interface Snapshot {
  sessionId: string;
  code: string;
  status: SessionStatus;
  version: number; // current event sequence
  currentSceneIndex: number;
  versionNo: number;
  scenes: Array<{ id: string; title: string; notes?: string }>;
  hostId: string;
  participants: Array<{ userId: string; name: string; role: string; joinedAt: string }>;
  handQueue: Array<{ userId: string; name: string; order: number; raisedAt: string }>;
  timeoutDeadline: string | null;
}

interface EventRow {
  id: string;
  sessionId: string;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  createdAt: Date;
}

type ConnectionState =
  | { kind: 'snapshot'; sessionId: string; snapshot: Snapshot }
  | { kind: 'events'; sessionId: string; events: EventRow[]; lastSeq: number };

@Injectable()
export class SessionsService {
  private readonly logger = new Logger(SessionsService.name);

  constructor(
    @Inject(DATA_SOURCE) private readonly ds: DataSource,
    private readonly config: AppConfig,
    @Inject(forwardRef(() => RealtimeGateway))
    private readonly gateway: RealtimeGateway,
  ) {}

  // ---------------------------------------------------------------- creation

  async create(dto: CreateSessionDto) {
    const version = await this.ds.manager.findOne(PresentationVersion, {
      where: { presentationId: dto.presentationId, version: dto.version },
    });
    if (!version) throw Errors.notFound('published version');

    const result = await this.ds.transaction(async (manager) => {
      // Retry on the rare unique-code collision.
      for (let attempt = 0; attempt < 5; attempt++) {
        const code = generateJoinCode();
        const existing = await manager.findOne(Session, {
          where: { code },
          select: { id: true },
        });
        if (existing) continue;

        const session = manager.create(Session, {
          id: randomUUID(),
          code,
          hostId: dto.hostId,
          presentationId: dto.presentationId,
          versionId: version.id,
          versionNo: version.version,
          status: 'lobby',
          currentSceneIndex: 0,
          maxParticipants: this.config.maxParticipants,
          lastEventSeq: '0',
          timeoutDeadline: new Date(Date.now() + this.config.sessionIdleTimeoutMs),
        });
        await manager.save(session);

        const host = manager.create(Participant, {
          id: randomUUID(),
          sessionId: session.id,
          userId: dto.hostId,
          name: dto.hostName,
          role: ROLE_HOST,
        });
        await manager.save(host);

        return { session, scenes: version.scenes };
      }
      throw Errors.badRequest('Could not allocate a join code, please retry');
    });

    const snapshot = await this.buildSnapshot(this.ds.manager, result.session.id);
    return {
      id: result.session.id,
      code: result.session.code,
      hostId: result.session.hostId,
      versionId: result.session.versionId,
      versionNo: result.session.versionNo,
      status: result.session.status,
      scenes: result.scenes,
      snapshot,
    };
  }

  async findByCode(code: string): Promise<Session> {
    const normalized = code.trim().toUpperCase();
    const session = await this.ds.manager.findOneBy(Session, { code: normalized });
    if (!session) throw Errors.notFound('session');
    return session;
  }

  // ------------------------------------------------------------------ join

  async join(code: string, dto: JoinSessionDto) {
    const normalized = code.trim().toUpperCase();
    const { session, participant, snapshot } = await this.ds.transaction(async (manager) => {
      // Row lock + count check inside one transaction: concurrent joins
      // serialise here, so the 25-seat cap can never be exceeded.
      const session = await manager
        .getRepository(Session)
        .createQueryBuilder('s')
        .setLock('pessimistic_write')
        .where('s.code = :code', { code: normalized })
        .getOne();
      if (!session) throw Errors.notFound('session');
      if (session.status === 'ended') throw Errors.ended();

      const existing = await manager.findOneBy(Participant, {
        sessionId: session.id,
        userId: dto.userId,
      });
      if (existing) {
        // Rejoin of the same user is idempotent and does not consume a seat
        // and does not extend the inactivity deadline.
        return { session, participant: existing, snapshot: null };
      }

      const count = await manager.count(Participant, {
        where: { sessionId: session.id },
      });
      if (count >= session.maxParticipants) throw Errors.sessionFull();

      const participant = manager.create(Participant, {
        id: randomUUID(),
        sessionId: session.id,
        userId: dto.userId,
        name: dto.name,
        role: session.hostId === dto.userId ? ROLE_HOST : ROLE_PARTICIPANT,
      });
      await manager.save(participant);

      // Joining is valid activity: slide the deadline forward.
      session.timeoutDeadline = new Date(Date.now() + this.config.sessionIdleTimeoutMs);
      await manager.save(session);

      const snapshot = await this.buildSnapshot(manager, session.id);
      return { session, participant, snapshot };
    });

    if (snapshot) {
      // The seat-count change is delivered through the snapshot channel.
      this.gateway.broadcastSnapshot(session.id, snapshot);
    }
    return {
      sessionId: session.id,
      participantId: participant.id,
      role: participant.role,
      snapshot: snapshot ?? (await this.buildSnapshot(this.ds.manager, session.id)),
    };
  }

  // --------------------------------------------------------------- commands

  async executeCommand(code: string, dto: CommandDto) {
    const normalized = code.trim().toUpperCase();
    const bodyHash = hashBody({
      command: dto.command,
      sceneIndex: dto.sceneIndex ?? null,
      name: dto.name ?? null,
    });

    // All work for one session serialises on the session row lock. The
    // idempotency lookup happens under the same lock, so a retry racing the
    // original request blocks until the original commits, then replays it.
    const outcome = await this.ds.transaction(async (manager) => {
      const session = await this.lockSessionByCode(manager, normalized);

      const existing = await manager.findOne(CommandRecord, {
        where: { sessionId: session.id, requestId: dto.requestId },
      });
      if (existing) {
        if (existing.bodyHash !== bodyHash || existing.command !== dto.command) {
          throw Errors.idempotencyConflict();
        }
        return { mode: 'replay' as const, result: existing.result, sessionId: session.id };
      }

      // First execution: permission, expected-version and state machine all
      // validated under the lock.
      const actorId = this.assertActor(dto);
      const participant = await manager.findOneBy(Participant, {
        sessionId: session.id,
        userId: actorId,
      });
      if (!participant) throw Errors.forbidden('You have not joined this session');

      const currentVersion = Number(session.lastEventSeq);
      if (dto.expectedVersion !== currentVersion) {
        throw Errors.versionConflict(dto.expectedVersion, currentVersion);
      }

      const applied = await this.applyCommand(manager, session, participant, dto);

      const record = manager.create(CommandRecord, {
        id: randomUUID(),
        sessionId: session.id,
        requestId: dto.requestId,
        command: dto.command,
        bodyHash,
        result: applied.result,
      });
      await manager.save(record);

      return {
        mode: 'applied' as const,
        result: applied.result,
        sessionId: session.id,
        events: applied.events,
        snapshot: applied.snapshotChanged
          ? await this.buildSnapshot(manager, session.id)
          : null,
      };
    });

    if (outcome.mode === 'applied') {
      // Broadcast happens strictly AFTER commit and its failure must never
      // turn a committed command into an error — clients recover via
      // catch-up. (This is also what makes "crash before broadcast" safe.)
      for (const event of outcome.events ?? []) {
        try {
          this.gateway.broadcastEvent(outcome.sessionId, this.toWireEvent(event));
        } catch (error) {
          this.logger.error(`Broadcast failed after commit for seq ${event.seq}: ${String(error)}`);
        }
      }
      if (outcome.snapshot) {
        try {
          this.gateway.broadcastSnapshot(outcome.sessionId, outcome.snapshot);
        } catch (error) {
          this.logger.error(`Snapshot broadcast failed after commit: ${String(error)}`);
        }
      }
    }
    return outcome.result;
  }

  private assertActor(dto: CommandDto): string {
    // The HTTP layer supplies the acting user via x-user-id; command bodies
    // never carry the identity (a client must not impersonate another user).
    const actor = (dto as CommandDto & { actorId?: string }).actorId;
    if (!actor) throw Errors.forbidden('Missing actor identity');
    return actor;
  }

  private async lockSessionByCode(manager: EntityManager, code: string): Promise<Session> {
    const session = await manager
      .getRepository(Session)
      .createQueryBuilder('s')
      .setLock('pessimistic_write')
      .where('s.code = :code', { code })
      .getOne();
    if (!session) throw Errors.notFound('session');
    return session;
  }

  private async applyCommand(
    manager: EntityManager,
    session: Session,
    participant: Participant,
    dto: CommandDto,
  ): Promise<{
    result: Record<string, unknown>;
    events: EventRow[];
    snapshotChanged: boolean;
  }> {
    const isHost = participant.role === ROLE_HOST;
    const events: EventRow[] = [];
    let snapshotChanged = false;

    const requireHost = () => {
      if (!isHost) throw Errors.forbidden('Only the host may perform this action');
    };
    const requireNotEnded = () => {
      if (session.status === 'ended') throw Errors.ended();
    };
    // Valid, authorised activity: move the auto-end deadline forward. Requests
    // that fail permission/state checks never reach here, so they cannot keep
    // an idle session alive.
    const touch = () => {
      session.timeoutDeadline = new Date(Date.now() + this.config.sessionIdleTimeoutMs);
    };
    const changeStatus = async (
      type: string,
      nextStatus: SessionStatus,
      detail: Record<string, unknown> = {},
    ) => {
      requireNotEnded();
      session.status = nextStatus;
      if (nextStatus === 'ended') {
        session.endedAt = new Date();
        session.timeoutDeadline = null;
      } else {
        touch();
      }
      await manager.save(session);
      events.push(await this.appendEvent(manager, session, type, detail));
      snapshotChanged = true;
    };

    switch (dto.command) {
      case 'start':
        requireHost();
        requireNotEnded();
        if (session.status !== 'lobby' && session.status !== 'paused') {
          throw Errors.invalidTransition(session.status, 'start');
        }
        await changeStatus(
          session.status === 'paused' ? 'session.resumed' : 'session.started',
          'live',
          { status: 'live' },
        );
        break;

      case 'pause':
        requireHost();
        if (session.status !== 'live') {
          throw Errors.invalidTransition(session.status, 'pause');
        }
        await changeStatus('session.paused', 'paused', { status: 'paused' });
        break;

      case 'resume':
        requireHost();
        if (session.status !== 'paused') {
          throw Errors.invalidTransition(session.status, 'resume');
        }
        await changeStatus('session.resumed', 'live', { status: 'live' });
        break;

      case 'end':
        requireHost();
        requireNotEnded();
        await changeStatus('session.ended', 'ended', {
          status: 'ended',
          reason: 'host',
        });
        break;

      case 'changeScene': {
        requireHost();
        requireNotEnded();
        if (dto.sceneIndex === undefined) throw Errors.badRequest('sceneIndex is required');
        const version = await manager.findOneBy(PresentationVersion, { id: session.versionId });
        if (!version) throw Errors.notFound('version');
        if (dto.sceneIndex >= version.scenes.length) {
          throw Errors.badRequest(
            `Scene index ${dto.sceneIndex} does not exist in the bound version`,
          );
        }
        if (session.status === 'lobby') {
          throw Errors.invalidTransition('lobby', 'changeScene');
        }
        session.currentSceneIndex = dto.sceneIndex;
        touch();
        await manager.save(session);
        events.push(
          await this.appendEvent(manager, session, 'scene.changed', {
            sceneIndex: dto.sceneIndex,
            scene: version.scenes[dto.sceneIndex],
          }),
        );
        snapshotChanged = true;
        break;
      }

      case 'raiseHand': {
        requireNotEnded();
        // Per-session queue counter. The unique (session_id,user_id) index is
        // the hard guarantee that a duplicate raise never enqueues twice.
        const existing = await manager.findOneBy(HandRaise, {
          sessionId: session.id,
          userId: participant.userId,
        });
        if (existing) {
          return {
            result: { ok: true, idempotent: true, handId: existing.id, order: Number(existing.serverOrder) },
            events: [],
            snapshotChanged: false,
          };
        }
        const nextOrder = Number(session.handCounter) + 1;
        session.handCounter = String(nextOrder);
        await manager.update(Session, session.id, { handCounter: String(nextOrder) });
        const hand = manager.create(HandRaise, {
          id: randomUUID(),
          sessionId: session.id,
          userId: participant.userId,
          name: dto.name?.slice(0, 200) || participant.name,
          serverOrder: String(nextOrder),
        });
        await manager.save(hand);
        touch();
        await manager.save(session);
        events.push(
          await this.appendEvent(manager, session, 'hand.raised', {
            handId: hand.id,
            userId: hand.userId,
            name: hand.name,
            order: nextOrder,
          }),
        );
        snapshotChanged = true;
        break;
      }

      case 'cancelHand': {
        requireNotEnded();
        const hand = await manager.findOneBy(HandRaise, {
          sessionId: session.id,
          userId: participant.userId,
        });
        if (!hand) {
          // Cancelling a non-existent raise is a no-op success (safe retry).
          return { result: { ok: true, idempotent: true, removed: false }, events: [], snapshotChanged: false };
        }
        const order = Number(hand.serverOrder);
        await manager.remove(hand);
        touch();
        await manager.save(session);
        events.push(
          await this.appendEvent(manager, session, 'hand.cancelled', {
            handId: hand.id,
            userId: participant.userId,
            order,
          }),
        );
        snapshotChanged = true;
        break;
      }

      case 'handleHand': {
        // Host removes a raised hand from the queue.
        requireHost();
        requireNotEnded();
        const targetUserId = dto.targetUserId;
        if (!targetUserId) throw Errors.badRequest('targetUserId is required for handleHand');
        const hand = await manager.findOneBy(HandRaise, {
          sessionId: session.id,
          userId: targetUserId,
        });
        if (!hand) {
          return { result: { ok: true, idempotent: true, removed: false }, events: [], snapshotChanged: false };
        }
        const order = Number(hand.serverOrder);
        const removedUser = hand.userId;
        await manager.remove(hand);
        touch();
        await manager.save(session);
        events.push(
          await this.appendEvent(manager, session, 'hand.handled', {
            handId: hand.id,
            userId: removedUser,
            order,
            handledBy: participant.userId,
          }),
        );
        snapshotChanged = true;
        break;
      }

      default:
        throw Errors.badRequest(`Unknown command: ${dto.command}`);
    }

    const seq = Number(session.lastEventSeq);
    return {
      result: {
        ok: true,
        command: dto.command,
        requestId: dto.requestId,
        version: seq,
      },
      events,
      snapshotChanged,
    };
  }

  // Append at last_event_seq+1 and advance the counter in the SAME update, so
  // state change and sequence advance commit atomically.
  private async appendEvent(
    manager: EntityManager,
    session: Session,
    type: string,
    payload: Record<string, unknown>,
  ): Promise<EventRow> {
    const next = Number(session.lastEventSeq) + 1;
    session.lastEventSeq = String(next);
    await manager.update(Session, session.id, { lastEventSeq: String(next) });

    const event = manager.create(SessionEvent, {
      id: randomUUID(),
      sessionId: session.id,
      seq: String(next),
      type,
      payload: { ...payload, version: next },
    });
    await manager.save(event);
    return {
      id: event.id,
      sessionId: event.sessionId,
      seq: next,
      type: event.type,
      payload: event.payload,
      createdAt: new Date(),
    };
  }

  // ---------------------------------------------------------------- timeout

  // Durable inactivity sweep. Deadline lives in the sessions table, so after a
  // restart this simply picks up where it left off. Race with a host command
  // is resolved on the row lock: whichever transaction locks the session first
  // wins; the loser re-reads and observes the new state and does nothing.
  async sweepTimeouts(): Promise<number> {
    const due = await this.ds.manager
      .getRepository(Session)
      .createQueryBuilder('s')
      .where('s.status != :ended', { ended: 'ended' })
      .andWhere('s.timeout_deadline IS NOT NULL')
      .andWhere('s.timeout_deadline <= now()')
      .getMany();

    let ended = 0;
    for (const candidate of due) {
      try {
        const result = await this.ds.transaction(async (manager) => {
          const session = await manager
            .getRepository(Session)
            .createQueryBuilder('s')
            .setLock('pessimistic_write')
            .where('s.id = :id', { id: candidate.id })
            .getOne();
          if (!session) return null;
          // Re-check under the lock: a host command or another sweeper may
          // have already moved the deadline / ended the session.
          if (session.status === 'ended' || !session.timeoutDeadline) return null;
          if (session.timeoutDeadline.getTime() > Date.now()) return null;

          session.status = 'ended';
          session.endedAt = new Date();
          session.timeoutDeadline = null;
          await manager.save(session);
          const event = await this.appendEvent(manager, session, 'session.ended', {
            status: 'ended',
            reason: 'inactivity_timeout',
          });
          const snapshot = await this.buildSnapshot(manager, session.id);
          return { sessionId: session.id, event, snapshot };
        });

        if (result) {
          this.gateway.broadcastEvent(result.sessionId, this.toWireEvent(result.event));
          this.gateway.broadcastSnapshot(result.sessionId, result.snapshot);
          ended += 1;
        }
      } catch (error) {
        this.logger.error(`Timeout sweep failed for session ${candidate.id}: ${String(error)}`);
      }
    }
    return ended;
  }

  // ------------------------------------------------------------- read model

  async getConnectionState(
    sessionIdOrCode: string,
    userId: string,
    lastSeq: number | null,
  ): Promise<ConnectionState> {
    return this.ds.transaction(async (manager) => {
      const session = await this.resolveSession(manager, sessionIdOrCode);
      if (!session) throw Errors.notFound('session');

      // Socket auth must name a joined participant. This check deliberately
      // does NOT touch timeout_deadline: merely connecting must not count as
      // activity.
      if (!userId) throw Errors.forbidden('Missing userId');
      const participant = await manager.findOneBy(Participant, {
        sessionId: session.id,
        userId,
      });
      if (!participant) throw Errors.forbidden('You have not joined this session');

      const currentVersion = Number(session.lastEventSeq);
      if (lastSeq === null || lastSeq < 0 || lastSeq > currentVersion) {
        return {
          kind: 'snapshot',
          sessionId: session.id,
          snapshot: await this.buildSnapshot(manager, session.id),
        };
      }

      if (lastSeq === currentVersion) {
        return { kind: 'events', sessionId: session.id, events: [], lastSeq: currentVersion };
      }

      const rows = await manager.find(SessionEvent, {
        where: { sessionId: session.id },
        order: { seq: 'ASC' },
        skip: lastSeq,
        take: 500,
      });
      const events = rows.map((r) => ({
        id: r.id,
        sessionId: r.sessionId,
        seq: Number(r.seq),
        type: r.type,
        payload: r.payload,
        createdAt: r.createdAt,
      }));

      // Gap (e.g. missing history in a deployment with retention): fall back
      // to a complete snapshot carrying the current version.
      const expectedFirst = lastSeq + 1;
      if (events.length === 0 || events[0].seq !== expectedFirst) {
        return {
          kind: 'snapshot',
          sessionId: session.id,
          snapshot: await this.buildSnapshot(manager, session.id),
        };
      }
      return { kind: 'events', sessionId: session.id, events, lastSeq: currentVersion };
    });
  }

  private isUuid(value: string): boolean {
    return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      value,
    );
  }

  private async resolveSession(manager: EntityManager, idOrCode: string): Promise<Session | null> {
    const value = idOrCode.trim();
    if (this.isUuid(value)) {
      const byId = await manager.findOneBy(Session, { id: value });
      if (byId) return byId;
    }
    return manager.findOneBy(Session, { code: value.toUpperCase() });
  }

  async snapshotByCode(code: string, userId: string) {
    const state = await this.getConnectionState(code.trim().toUpperCase(), userId, null);
    if (state.kind !== 'snapshot') throw Errors.notFound('snapshot');
    return state.snapshot;
  }

  async eventsByCode(code: string, userId: string, afterSeq: number) {
    const state = await this.getConnectionState(code.trim().toUpperCase(), userId, afterSeq);
    return state;
  }

  async buildSnapshot(manager: EntityManager, sessionId: string): Promise<Snapshot> {
    const session = await manager.findOneBy(Session, { id: sessionId });
    if (!session) throw Errors.notFound('session');
    const version = await manager.findOneBy(PresentationVersion, { id: session.versionId });
    const participants = await manager.find(Participant, {
      where: { sessionId },
      order: { joinedAt: 'ASC' },
    });
    const hands = await manager.find(HandRaise, {
      where: { sessionId },
      order: { serverOrder: 'ASC' },
    });

    return {
      sessionId: session.id,
      code: session.code,
      status: session.status,
      version: Number(session.lastEventSeq),
      currentSceneIndex: session.currentSceneIndex,
      versionNo: session.versionNo,
      scenes: version?.scenes ?? [],
      hostId: session.hostId,
      participants: participants.map((p) => ({
        userId: p.userId,
        name: p.name,
        role: p.role,
        joinedAt: p.joinedAt.toISOString(),
      })),
      handQueue: hands.map((h) => ({
        userId: h.userId,
        name: h.name,
        order: Number(h.serverOrder),
        raisedAt: h.raisedAt.toISOString(),
      })),
      timeoutDeadline: session.timeoutDeadline ? session.timeoutDeadline.toISOString() : null,
    };
  }

  private toWireEvent(row: EventRow) {
    return {
      sessionId: row.sessionId,
      seq: row.seq,
      type: row.type,
      payload: row.payload,
      createdAt: (row.createdAt instanceof Date ? row.createdAt : new Date()).toISOString(),
    };
  }
}
