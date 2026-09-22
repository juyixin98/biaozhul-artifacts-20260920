import { Injectable, NotFoundException } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { HandRaise } from '../entities/hand-raise.entity';
import { Participant } from '../entities/participant.entity';
import { PresentationVersion } from '../entities/presentation-version.entity';
import { Session } from '../entities/session.entity';

/**
 * Builds the full session snapshot sent to clients that are missing history
 * (or joining for the first time). Shared by the REST controller and the
 * WebSocket gateway.
 */
@Injectable()
export class SnapshotService {
  constructor(
    @InjectRepository(Session)
    private readonly sessions: Repository<Session>,
    @InjectRepository(Participant)
    private readonly participants: Repository<Participant>,
    @InjectRepository(PresentationVersion)
    private readonly versions: Repository<PresentationVersion>,
    @InjectRepository(HandRaise)
    private readonly handRaises: Repository<HandRaise>,
  ) {}

  async snapshot(sessionId: string) {
    const session = await this.sessions.findOne({ where: { id: sessionId } });
    if (!session) throw new NotFoundException('session not found');
    const [version, participants, raisedHands] = await Promise.all([
      this.versions.findOne({
        where: { id: session.presentationVersionId },
      }),
      this.participants.find({
        where: { sessionId },
        order: { joinedAt: 'ASC' },
      }),
      this.handRaises.find({
        where: { sessionId, status: 'raised' },
        order: { id: 'ASC' },
      }),
    ]);
    const nameById = new Map(participants.map((p) => [p.id, p.name]));
    return {
      sessionId: session.id,
      joinCode: session.joinCode,
      status: session.status,
      currentSceneIndex: session.currentSceneIndex,
      version: session.version,
      eventSeq: session.eventSeq,
      presentationVersionId: session.presentationVersionId,
      scenes: version ? version.scenes : [],
      participants: participants.map((p) => ({
        id: p.id,
        name: p.name,
        role: p.role,
      })),
      raisedHands: raisedHands.map((h) => ({
        id: h.id,
        participantId: h.participantId,
        name: nameById.get(h.participantId) ?? null,
        raisedAt: h.createdAt,
      })),
    };
  }
}
