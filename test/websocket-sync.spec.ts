import { INestApplication } from '@nestjs/common';
import { io, Socket } from 'socket.io-client';
import request from 'supertest';
import { createTestApp, listenEphemeral, truncateAll, uniqueUser } from './helpers';
import { cmd, join, setupFlow } from './flow';
import { SessionsService } from '../src/sessions/sessions.service';
import { RealtimeGateway } from '../src/sessions/realtime.gateway';

async function connectWs(
  baseUrl: string,
  auth: { sessionId: string; userId: string; lastSeq?: number },
): Promise<{ socket: Socket; connected: any }> {
  const socket = io(baseUrl, { auth, transports: ['websocket'], forceNew: true });
  const connected = await new Promise<any>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('ws connect timeout')), 8000);
    socket.once('connected', (msg) => {
      clearTimeout(timer);
      resolve(msg);
    });
    socket.once('error', (err) => {
      clearTimeout(timer);
      reject(new Error(err.message ?? 'ws error'));
    });
  });
  return { socket, connected };
}

describe('websocket synchronisation', () => {
  let app: INestApplication;
  let baseUrl: string;

  beforeAll(async () => {
    app = await createTestApp();
    await app.init();
    const port = await listenEphemeral(app);
    baseUrl = `http://127.0.0.1:${port}`;
  });
  beforeEach(truncateAll);
  afterAll(() => app.close());

  it('streams live events with gapless seqs; a reconnecting client catches up from its last seq', async () => {
    const flow = await setupFlow(app);
    const viewer = uniqueUser('viewer');
    await join(app, flow.code, viewer).expect(201);

    const { socket } = await connectWs(baseUrl, {
      sessionId: flow.code,
      userId: viewer.userId,
      lastSeq: -1,
    });

    const received: number[] = [];
    socket.on('event', (e) => received.push(e.seq));

    await cmd(app, flow.code, flow.hostId, { command: 'start', expectedVersion: 0 }).expect(201);
    await cmd(app, flow.code, flow.hostId, { command: 'changeScene', expectedVersion: 1, sceneIndex: 1 }).expect(201);

    await new Promise((r) => setTimeout(r, 200));
    expect(received).toEqual([1, 2]);
    socket.disconnect();

    // Client "missed" events while disconnected; a third event happens.
    await cmd(app, flow.code, flow.hostId, { command: 'changeScene', expectedVersion: 2, sceneIndex: 2 }).expect(201);

    // Reconnect claiming lastSeq=2: only event 3 comes back.
    const reconnect = await connectWs(baseUrl, {
      sessionId: flow.code,
      userId: viewer.userId,
      lastSeq: 2,
    });
    expect(reconnect.connected.kind).toBe('events');
    expect(reconnect.connected.events.map((e: { seq: number }) => e.seq)).toEqual([3]);
    reconnect.socket.disconnect();
  });

  it('sends a complete snapshot when the client requests an unreachable/old point', async () => {
    const flow = await setupFlow(app);
    const viewer = uniqueUser('viewer');
    await join(app, flow.code, viewer).expect(201);

    await cmd(app, flow.code, flow.hostId, { command: 'start', expectedVersion: 0 }).expect(201);

    // lastSeq ahead of the server is impossible: full snapshot instead.
    const { socket, connected } = await connectWs(baseUrl, {
      sessionId: flow.code,
      userId: viewer.userId,
      lastSeq: 999,
    });
    expect(connected.kind).toBe('snapshot');
    expect(connected.snapshot.version).toBe(1);
    expect(connected.snapshot.status).toBe('live');
    socket.disconnect();
  });

  it('rejects socket connections from users who never joined', async () => {
    const flow = await setupFlow(app);
    await expect(
      connectWs(baseUrl, { sessionId: flow.code, userId: 'stranger', lastSeq: -1 }),
    ).rejects.toThrow(/joined/i);
  });

  // CRASH-BEFORE-BROADCAST: the DB transaction commits (durable event at the
  // next seq) but the broadcast step fails. The next client must still
  // receive the event through catch-up — broadcast is never the source of
  // truth.
  it('keeps events durable when the broadcast fails (crash before broadcast)', async () => {
    const flow = await setupFlow(app);
    const viewer = uniqueUser('viewer');
    await join(app, flow.code, viewer).expect(201);

    const gateway = app.get(RealtimeGateway);
    const original = gateway.broadcastEvent.bind(gateway);
    gateway.broadcastEvent = () => {
      throw new Error('simulated crash before broadcast');
    };

    const res = await cmd(app, flow.code, flow.hostId, {
      command: 'start',
      expectedVersion: 0,
    }).expect(201);
    expect(res.body.version).toBe(1);

    // Idempotent retry also still works.
    gateway.broadcastEvent = original;
    const retry = await request(app.getHttpServer())
      .post(`/sessions/${flow.code}/commands`)
      .set('x-user-id', flow.hostId)
      .send({ command: 'start', expectedVersion: 0, requestId: res.body.requestId })
      .expect(201);
    expect(retry.body.version).toBe(1);

    // A fresh client catches up the "never broadcast" event over HTTP.
    const events = await request(app.getHttpServer())
      .get(`/sessions/${flow.code}/events?afterSeq=0`)
      .set('x-user-id', viewer.userId)
      .expect(200);
    expect(events.body.kind).toBe('events');
    expect(events.body.events.map((e: { type: string }) => e.type)).toContain('session.started');

    // And over a new WS connection from seq 0.
    const reconnect = await connectWs(baseUrl, {
      sessionId: flow.code,
      userId: viewer.userId,
      lastSeq: 0,
    });
    expect(reconnect.connected.events[0].type).toBe('session.started');
    reconnect.socket.disconnect();
  });

  it('explicit catchup message after a gap returns the missed events', async () => {
    const flow = await setupFlow(app);
    const viewer = uniqueUser('viewer');
    await join(app, flow.code, viewer).expect(201);

    const { socket } = await connectWs(baseUrl, {
      sessionId: flow.code,
      userId: viewer.userId,
      lastSeq: -1,
    });
    await cmd(app, flow.code, flow.hostId, { command: 'start', expectedVersion: 0 }).expect(201);
    await new Promise((r) => setTimeout(r, 150));

    const state = await socket.emitWithAck('catchup', { lastSeq: 0 });
    expect(state.kind).toBe('events');
    expect(state.events).toHaveLength(1);
    socket.disconnect();
  });
});
