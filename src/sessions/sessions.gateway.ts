import { Logger } from '@nestjs/common';
import {
  OnGatewayConnection,
  WebSocketGateway,
  WebSocketServer,
} from '@nestjs/websockets';
import { InjectRepository } from '@nestjs/typeorm';
import { MoreThan, Repository } from 'typeorm';
import { Server, WebSocket } from 'ws';
import { Participant } from '../entities/participant.entity';
import { SessionEvent } from '../entities/session-event.entity';
import { Session } from '../entities/session.entity';
import { SnapshotService } from './snapshot.service';

interface SubscribedClient extends WebSocket {
  sessionId?: string;
  lastSeq?: number;
  isAlive?: boolean;
}

/**
 * WebSocket sync channel (path /ws).
 *
 * Protocol (JSON):
 *   client -> { type: 'subscribe', sessionId, token, lastSeq }
 *   server -> { type: 'snapshot', seq, state }            (history gap / first join)
 *   server -> { type: 'event', seq, eventType, payload }  (ordered catch-up + live)
 *   server -> { type: 'subscribed', sessionId, seq }
 *   server -> { type: 'error', code, message }
 *
 * Catch-up: events with seq > lastSeq are replayed from the durable
 * session_events log. If history is missing (lastSeq = 0, a gap, or a
 * lastSeq ahead of the server) a full snapshot with the current seq is
 * sent instead. Clients must ignore events with seq <= last applied seq so
 * duplicate or out-of-order deliveries never roll state back.
 */
@WebSocketGateway({ path: '/ws' })
export class SessionsGateway implements OnGatewayConnection {
  private readonly logger = new Logger(SessionsGateway.name);

  @WebSocketServer()
  server: Server;

  private readonly clients = new Map<string, Set<SubscribedClient>>();

  constructor(
    @InjectRepository(Session)
    private readonly sessions: Repository<Session>,
    @InjectRepository(Participant)
    private readonly participants: Repository<Participant>,
    @InjectRepository(SessionEvent)
    private readonly events: Repository<SessionEvent>,
    private readonly snapshots: SnapshotService,
  ) {}

  handleConnection(client: SubscribedClient) {
    client.on('message', (raw: Buffer) => {
      this.onMessage(client, raw).catch((e) => {
        this.logger.warn(`message handling failed: ${e}`);
        this.send(client, { type: 'error', code: 'INTERNAL', message: String(e) });
      });
    });
    client.on('close', () => this.removeClient(client));
    client.on('error', () => this.removeClient(client));
  }

  private async onMessage(client: SubscribedClient, raw: Buffer) {
    let msg: any;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      this.send(client, { type: 'error', code: 'BAD_JSON', message: 'invalid JSON' });
      return;
    }
    if (msg?.type !== 'subscribe') {
      this.send(client, {
        type: 'error',
        code: 'BAD_MESSAGE',
        message: 'first message must be subscribe',
      });
      return;
    }

    const { sessionId, token } = msg;
    const lastSeq = Number(msg.lastSeq ?? 0);
    const participant =
      sessionId && token
        ? await this.participants.findOne({ where: { sessionId, token } })
        : null;
    if (!participant) {
      this.send(client, {
        type: 'error',
        code: 'UNAUTHORIZED',
        message: 'invalid session or token',
      });
      client.close();
      return;
    }
    const session = await this.sessions.findOne({ where: { id: sessionId } });
    if (!session) {
      this.send(client, {
        type: 'error',
        code: 'NOT_FOUND',
        message: 'session not found',
      });
      client.close();
      return;
    }

    this.addClient(sessionId, client);

    // Decide between ordered replay and full snapshot.
    const missed = await this.events.find({
      where: { sessionId, seq: MoreThan(Math.max(lastSeq, 0)) },
      order: { seq: 'ASC' },
      take: 1000,
    });
    const historyGap =
      missed.length > 0 && missed[0].seq !== lastSeq + 1;
    const needsSnapshot =
      lastSeq <= 0 || lastSeq > session.eventSeq || historyGap;

    if (needsSnapshot) {
      const state = await this.snapshots.snapshot(sessionId);
      this.send(client, { type: 'snapshot', seq: session.eventSeq, state });
    } else {
      for (const event of missed) {
        this.sendEvent(client, event);
      }
    }
    client.lastSeq = session.eventSeq;
    this.send(client, {
      type: 'subscribed',
      sessionId,
      seq: session.eventSeq,
      role: participant.role,
      participantId: participant.id,
    });
  }

  /** Called by services AFTER the state change transaction has committed. */
  broadcast(sessionId: string, event: SessionEvent) {
    const set = this.clients.get(sessionId);
    if (!set) return;
    for (const client of set) {
      if (client.readyState !== client.OPEN) continue;
      // Never deliver an event at or below what the client already applied.
      if (client.lastSeq !== undefined && event.seq <= client.lastSeq) continue;
      this.sendEvent(client, event);
      client.lastSeq = event.seq;
    }
  }

  private sendEvent(client: SubscribedClient, event: SessionEvent) {
    this.send(client, {
      type: 'event',
      seq: event.seq,
      eventType: event.type,
      payload: event.payload,
    });
  }

  private send(client: WebSocket, msg: Record<string, unknown>) {
    if (client.readyState === client.OPEN) {
      client.send(JSON.stringify(msg));
    }
  }

  private addClient(sessionId: string, client: SubscribedClient) {
    this.removeClient(client);
    client.sessionId = sessionId;
    let set = this.clients.get(sessionId);
    if (!set) {
      set = new Set();
      this.clients.set(sessionId, set);
    }
    set.add(client);
  }

  private removeClient(client: SubscribedClient) {
    if (!client.sessionId) return;
    const set = this.clients.get(client.sessionId);
    if (set) {
      set.delete(client);
      if (set.size === 0) this.clients.delete(client.sessionId);
    }
    client.sessionId = undefined;
  }
}
