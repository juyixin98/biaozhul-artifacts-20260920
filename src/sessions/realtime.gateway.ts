import { Inject, Logger, forwardRef } from '@nestjs/common';
import {
  ConnectedSocket,
  MessageBody,
  OnGatewayConnection,
  SubscribeMessage,
  WebSocketGateway,
  WebSocketServer,
} from '@nestjs/websockets';
import { Server, Socket } from 'socket.io';
import { SessionsService } from './sessions.service';

export interface RealtimeEvent {
  sessionId: string;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  createdAt: string;
}

@WebSocketGateway({ cors: { origin: '*' } })
export class RealtimeGateway implements OnGatewayConnection {
  @WebSocketServer()
  server: Server;

  private readonly logger = new Logger(RealtimeGateway.name);

  constructor(
    @Inject(forwardRef(() => SessionsService))
    private readonly sessions: SessionsService,
  ) {}

  async handleConnection(client: Socket): Promise<void> {
    const auth = { ...(client.handshake.auth ?? {}), ...(client.handshake.query ?? {}) };
    const sessionId = String(auth.sessionId ?? '');
    const userId = String(auth.userId ?? '');
    const lastSeq = this.parseSeq(auth.lastSeq);

    try {
      const state = await this.sessions.getConnectionState(sessionId, userId, lastSeq);
      // Normalise a join code to the session id: broadcasts target the
      // session-id room.
      const resolvedSessionId = state.sessionId;
      client.data.sessionId = resolvedSessionId;
      client.data.userId = userId;
      client.join(this.room(resolvedSessionId));

      client.emit('connected', {
        sessionId,
        kind: state.kind,
        ...(state.kind === 'snapshot'
          ? { snapshot: state.snapshot, seq: state.snapshot.version }
          : { events: state.events, seq: state.lastSeq }),
        serverTime: new Date().toISOString(),
      });
    } catch (error) {
      client.emit('error', { message: error instanceof Error ? error.message : 'Connection rejected' });
      client.disconnect(true);
    }
  }

  // Explicit catch-up: clients call this with their last applied seq after a
  // dropped connection or when they detect a gap in the sequence.
  @SubscribeMessage('catchup')
  async onCatchup(
    @ConnectedSocket() client: Socket,
    @MessageBody() data: { lastSeq: number },
  ) {
    const sessionId: string = client.data.sessionId;
    if (!sessionId) return { error: 'NOT_CONNECTED' };
    const state = await this.sessions.getConnectionState(
      sessionId,
      client.data.userId,
      this.parseSeq(data?.lastSeq),
    );
    return state;
  }

  // Called after the DB transaction commits. If the process crashes before
  // this runs, the event is still durable and every reconnecting client
  // fetches it via catch-up — broadcast is a pure accelerator, never the
  // source of truth.
  broadcastEvent(sessionId: string, event: RealtimeEvent): void {
    try {
      this.server?.to(this.room(sessionId)).emit('event', event);
    } catch (error) {
      this.logger.error(
        `Failed to broadcast event ${event.seq} for session ${sessionId}: ${String(error)}`,
      );
    }
  }

  // Participant list / hand queue changes that clients read from snapshots.
  broadcastSnapshot(sessionId: string, snapshot: object): void {
    try {
      this.server?.to(this.room(sessionId)).emit('snapshot', { sessionId, snapshot });
    } catch (error) {
      this.logger.error(
        `Failed to broadcast snapshot for session ${sessionId}: ${String(error)}`,
      );
    }
  }

  private parseSeq(value: unknown): number | null {
    if (value === undefined || value === null || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) && n >= 0 ? n : null;
  }

  private room(sessionId: string): string {
    return `session:${sessionId}`;
  }
}
