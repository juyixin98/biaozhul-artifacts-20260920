import { INestApplication } from '@nestjs/common';
import request from 'supertest';
import { createTestApp, truncateAll, uniqueUser } from './helpers';
import { cmd, join, setupFlow, snapshot } from './flow';

describe('commands: idempotency, optimistic version, state machine, permissions', () => {
  let app: INestApplication;

  beforeAll(async () => {
    app = await createTestApp();
    await app.init();
  });
  beforeEach(truncateAll);
  afterAll(() => app.close());

  it('replays the original result for a retried requestId, even after the version advanced', async () => {
    const flow = await setupFlow(app);

    const start = await cmd(app, flow.code, flow.hostId, {
      command: 'start',
      expectedVersion: 0,
      requestId: 'fixed-req-1',
    }).expect(201);
    expect(start.body.version).toBe(1);

    // Move the world forward to version 2.
    await cmd(app, flow.code, flow.hostId, {
      command: 'changeScene',
      expectedVersion: 1,
      sceneIndex: 1,
      requestId: 'other-req',
    }).expect(201);

    // Exact retry of the start command with the ORIGINAL expected version:
    // must replay the stored result instead of raising a version conflict.
    const retry = await cmd(app, flow.code, flow.hostId, {
      command: 'start',
      expectedVersion: 0,
      requestId: 'fixed-req-1',
    }).expect(201);
    expect(retry.body).toEqual(start.body);
  });

  it('rejects a reused requestId carrying a different body', async () => {
    const flow = await setupFlow(app);
    await cmd(app, flow.code, flow.hostId, {
      command: 'start',
      expectedVersion: 0,
      requestId: 'dup-req',
    }).expect(201);

    const conflict = await cmd(app, flow.code, flow.hostId, {
      command: 'pause',
      expectedVersion: 1,
      requestId: 'dup-req',
    }).expect(409);
    expect(conflict.body.error).toBe('IDEMPOTENCY_CONFLICT');
  });

  it('rejects stale expectedVersion (optimistic concurrency)', async () => {
    const flow = await setupFlow(app);
    await cmd(app, flow.code, flow.hostId, { command: 'start', expectedVersion: 0 }).expect(201);

    const stale = await cmd(app, flow.code, flow.hostId, {
      command: 'changeScene',
      expectedVersion: 0,
      sceneIndex: 1,
    }).expect(409);
    expect(stale.body.error).toBe('VERSION_CONFLICT');
  });

  it('forbids participants from host-only commands, and forbidden commands do not advance the version', async () => {
    const flow = await setupFlow(app);
    const participant = uniqueUser('p');
    await join(app, flow.code, participant).expect(201);

    await cmd(app, flow.code, participant.userId, {
      command: 'start',
      expectedVersion: 0,
    }).expect(403);

    const snap = await snapshot(app, flow.code, flow.hostId).expect(200);
    expect(snap.body.status).toBe('lobby');
    expect(snap.body.version).toBe(0);
  });

  it('walks lobby -> live -> paused -> live -> ended and refuses to leave ended', async () => {
    const flow = await setupFlow(app);

    // changeScene in lobby is illegal.
    const illegal = await cmd(app, flow.code, flow.hostId, {
      command: 'changeScene',
      expectedVersion: 0,
      sceneIndex: 1,
    }).expect(409);
    expect(illegal.body.error).toBe('INVALID_TRANSITION');

    await cmd(app, flow.code, flow.hostId, { command: 'start', expectedVersion: 0 }).expect(201);
    await cmd(app, flow.code, flow.hostId, { command: 'changeScene', expectedVersion: 1, sceneIndex: 2 }).expect(201);
    expect((await snapshot(app, flow.code, flow.hostId)).body.currentSceneIndex).toBe(2);

    await cmd(app, flow.code, flow.hostId, { command: 'pause', expectedVersion: 2 }).expect(201);
    // Pausing twice is rejected.
    await cmd(app, flow.code, flow.hostId, { command: 'pause', expectedVersion: 3 }).expect(409);
    await cmd(app, flow.code, flow.hostId, { command: 'resume', expectedVersion: 3 }).expect(201);
    await cmd(app, flow.code, flow.hostId, { command: 'end', expectedVersion: 4 }).expect(201);

    const revive = await cmd(app, flow.code, flow.hostId, {
      command: 'start',
      expectedVersion: 5,
    }).expect(409);
    expect(revive.body.error).toBe('SESSION_ENDED');
  });

  it('queues hand raises in server reception order, dedupes, allows self-cancel only, host can handle', async () => {
    const flow = await setupFlow(app);
    await cmd(app, flow.code, flow.hostId, { command: 'start', expectedVersion: 0 }).expect(201);

    const a = uniqueUser('alice');
    const b = uniqueUser('bob');
    const c = uniqueUser('carol');
    await join(app, flow.code, a).expect(201);
    await join(app, flow.code, b).expect(201);
    await join(app, flow.code, c).expect(201);

    // Sequential raises: queue follows reception order.
    await cmd(app, flow.code, a.userId, { command: 'raiseHand', expectedVersion: 1, name: 'Alice' }).expect(201);
    await cmd(app, flow.code, b.userId, { command: 'raiseHand', expectedVersion: 2, name: 'Bob' }).expect(201);
    // Duplicate raise by same user: no second enqueue, no new event.
    const dup = await cmd(app, flow.code, a.userId, {
      command: 'raiseHand',
      expectedVersion: 3,
      name: 'Alice',
    }).expect(201);
    expect(dup.body.idempotent).toBe(true);

    let snap = (await snapshot(app, flow.code, flow.hostId)).body;
    expect(snap.handQueue.map((h: { userId: string }) => h.userId)).toEqual([a.userId, b.userId]);
    // No event for the duplicate: version still 3.
    expect(snap.version).toBe(3);

    // Carol cannot cancel Alice's hand — she has no hand, so this is a no-op
    // for her own queue, and Alice stays queued.
    await cmd(app, flow.code, c.userId, { command: 'cancelHand', expectedVersion: 3 }).expect(201);
    snap = (await snapshot(app, flow.code, flow.hostId)).body;
    expect(snap.handQueue.map((h: { userId: string }) => h.userId)).toEqual([a.userId, b.userId]);

    // Alice cancels her own hand.
    await cmd(app, flow.code, a.userId, { command: 'cancelHand', expectedVersion: 3 }).expect(201);
    snap = (await snapshot(app, flow.code, flow.hostId)).body;
    expect(snap.handQueue.map((h: { userId: string }) => h.userId)).toEqual([b.userId]);

    // Host handles Bob.
    await cmd(app, flow.code, flow.hostId, {
      command: 'handleHand',
      expectedVersion: 4,
      targetUserId: b.userId,
    }).expect(201);
    snap = (await snapshot(app, flow.code, flow.hostId)).body;
    expect(snap.handQueue).toEqual([]);

    // A participant cannot handle hands.
    await cmd(app, flow.code, a.userId, {
      command: 'handleHand',
      expectedVersion: 5,
      targetUserId: c.userId,
    }).expect(403);
  });

  it('requires identity header', async () => {
    const flow = await setupFlow(app);
    await request(app.getHttpServer())
      .post(`/sessions/${flow.code}/commands`)
      .send({ command: 'start', expectedVersion: 0, requestId: 'x' })
      .expect(401);
  });
});
