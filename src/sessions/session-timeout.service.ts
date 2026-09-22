import { Injectable, Logger } from '@nestjs/common';
import { Interval } from '@nestjs/schedule';
import { InjectDataSource } from '@nestjs/typeorm';
import { DataSource, LessThan, Not } from 'typeorm';
import { Session } from '../entities/session.entity';
import { SessionsGateway } from './sessions.gateway';
import { SessionsService } from './sessions.service';

/**
 * Ends sessions after 30 minutes without valid activity.
 *
 * Race boundary vs. host commands: the sweeper locks the session row and
 * re-checks (status != ended AND lastActivityAt < cutoff) inside the
 * transaction. A host command updates lastActivityAt in its own
 * transaction under the same row lock, so exactly one side wins:
 *   - command commits first  -> lastActivityAt is fresh -> sweeper skips
 *   - sweeper commits first  -> status is 'ended'      -> command rejected
 *
 * Because the condition is evaluated from durable state, a service restart
 * simply resumes sweeping on the next tick — no in-memory timers to lose.
 */
@Injectable()
export class SessionTimeoutService {
  private readonly logger = new Logger(SessionTimeoutService.name);

  constructor(
    @InjectDataSource() private readonly dataSource: DataSource,
    private readonly sessionsService: SessionsService,
    private readonly gateway: SessionsGateway,
  ) {}

  static timeoutMs(): number {
    return Number(process.env.SESSION_TIMEOUT_MS ?? 30 * 60 * 1000);
  }

  @Interval(15_000)
  async sweep() {
    if (process.env.SESSION_SWEEP_DISABLED === 'true') return;
    try {
      await this.sweepOnce();
    } catch (e) {
      this.logger.error(`timeout sweep failed: ${e}`);
    }
  }

  /** One sweep pass. Exposed for tests. Returns ids of sessions it ended. */
  async sweepOnce(): Promise<string[]> {
    const cutoff = new Date(Date.now() - SessionTimeoutService.timeoutMs());
    const candidates = await this.dataSource.getRepository(Session).find({
      where: { status: Not('ended'), lastActivityAt: LessThan(cutoff) },
      select: ['id'],
    });
    const ended: string[] = [];
    for (const candidate of candidates) {
      const event = await this.dataSource.transaction(async (manager) => {
        const session = await manager
          .getRepository(Session)
          .createQueryBuilder('s')
          .setLock('pessimistic_write')
          .where('s.id = :id', { id: candidate.id })
          .getOne();
        // Re-check under the row lock: a host command may have refreshed
        // activity (or ended the session) since the candidate scan.
        if (
          !session ||
          session.status === 'ended' ||
          session.lastActivityAt >= cutoff
        ) {
          return null;
        }
        session.status = 'ended';
        session.endedAt = new Date();
        const event = await this.sessionsService.appendEvent(
          manager,
          session,
          'session_ended',
          { reason: 'inactivity_timeout', status: 'ended' },
        );
        await manager.save(Session, session);
        return event;
      });
      if (event) {
        ended.push(candidate.id);
        try {
          this.gateway.broadcast(candidate.id, event);
        } catch (e) {
          this.logger.warn(`timeout broadcast failed: ${e}`);
        }
      }
    }
    return ended;
  }
}
