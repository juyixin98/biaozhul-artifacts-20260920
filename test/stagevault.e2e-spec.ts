import { DataSource } from 'typeorm';
import { Session } from '../src/entities/session.entity';
import { SessionEvent } from '../src/entities/session-event.entity';
import { SessionTimeoutService } from '../src/sessions/session-timeout.service';
import { SessionsGateway } from '../src/sessions/sessions.gateway';
import {
  cleanDatabase,
  createTestApp,
  makeApi,
  setupSession,
  TestContext,
  TestWsClient,
} from './helpers';

describe('StageVault (e2e)', () => {
  let ctx: TestContext;
  let api: ReturnType<typeof makeApi>;
  let dataSource: DataSource;

  beforeAll(async () => {
    ctx = await createTestApp();
    api = makeApi(ctx.baseUrl);
    dataSource = ctx.dataSource;
  });

  afterAll(async () => {
    await ctx.app.close();
  });

  beforeEach(async () => {
    await cleanDatabase(dataSource);
    jest.restoreAllMocks();
  });

  // ------------------------------------------------------------ presentations

  describe('presentations & versions', () => {
    it('rejects more than 50 scenes', async () => {
      const p = (await api('POST', '/presentations', { body: { title: 'x' } })).body;
      const scenes = Array.from({ length: 51 }, (_, i) => ({ title: `s${i}` }));
      const res = await api('POST', `/presentations/${p.id}/versions`, {
        body: { scenes },
      });
      expect(res.status).toBe(400);
    });

    it('published versions are immutable and sessions keep their bound version', async () => {
      const { presentation, version, session } = await setupSession(api, 2);

      // re-publish rejected
      const republish = await api(
        'POST',
        `/presentation-versions/${version.id}/publish`,
      );
      expect(republish.status).toBe(409);

      // a later edit creates a NEW version; the session still serves v1
      const v2 = (
        await api('POST', `/presentations/${presentation.id}/versions`, {
          body: { scenes: [{ title: 'edited' }] },
        })
      ).body;
      expect(v2.versionNumber).toBe(2);

      const state = (
        await api('GET', `/sessions/${session.sessionId}/state`, {
          token: session.hostToken,
        })
      ).body;
      expect(state.presentationVersionId).toBe(version.id);
      expect(state.scenes).toHaveLength(2);
    });

    it('sessions cannot bind to an unpublished version', async () => {
      const p = (await api('POST', '/presentations', { body: { title: 'x' } })).body;
      const v = (
        await api('POST', `/presentations/${p.id}/versions`, {
          body: { scenes: [{ title: 'a' }] },
        })
      ).body;
      const res = await api('POST', '/sessions', {
        body: { presentationVersionId: v.id, hostName: 'h' },
      });
      expect(res.status).toBe(400);
    });
  });

  // ------------------------------------------------------------------- joins

  describe('joins', () => {
    it('caps participants at 25 under concurrent joins', async () => {
      const { session } = await setupSession(api);
      // host already occupies one seat -> 24 seats left
      const results = await Promise.allSettled(
        Array.from({ length: 30 }, (_, i) =>
          api('POST', '/sessions/join', {
            body: { joinCode: session.joinCode, name: `p${i}` },
          }).then((r) => {
            if (r.status !== 200 && r.status !== 201) {
              throw Object.assign(new Error('rejected'), { status: r.status });
            }
            return r.body;
          }),
        ),
      );
      const ok = results.filter((r) => r.status === 'fulfilled');
      const rejected = results.filter((r) => r.status === 'rejected');
      expect(ok).toHaveLength(24);
      expect(rejected).toHaveLength(6);
      for (const r of rejected) {
        expect((r as PromiseRejectedResult).reason.status).toBe(409);
      }
      const state = (
        await api('GET', `/sessions/${session.sessionId}/state`, {
          token: session.hostToken,
        })
      ).body;
      expect(state.participants).toHaveLength(25);
    });

    it('rejects joins to ended sessions and bad codes', async () => {
      const { session } = await setupSession(api);
      await api('POST', `/sessions/${session.sessionId}/commands`, {
        token: session.hostToken,
        body: { requestId: 'end-1', expectedVersion: 0, type: 'end' },
      });
      const res = await api('POST', '/sessions/join', {
        body: { joinCode: session.joinCode, name: 'late' },
      });
      expect(res.status).toBe(409);
      expect(res.body.code ?? res.body.message).toBeDefined();
      const bad = await api('POST', '/sessions/join', {
        body: { joinCode: 'ZZZZZZ', name: 'nobody' },
      });
      expect(bad.status).toBe(404);
    });
  });

  // ----------------------------------------------------------------- commands

  describe('commands', () => {
    it('same requestId + same content returns the original result', async () => {
      const { session } = await setupSession(api);
      const cmd = {
        requestId: 'start-1',
        expectedVersion: 0,
        type: 'start',
      };
      const first = await api('POST', `/sessions/${session.sessionId}/commands`, {
        token: session.hostToken,
        body: cmd,
      });
      expect(first.status).toBe(201);
      expect(first.body.replayed).toBe(false);

      const retry = await api('POST', `/sessions/${session.sessionId}/commands`, {
        token: session.hostToken,
        body: cmd,
      });
      expect(retry.status).toBe(201);
      expect(retry.body.replayed).toBe(true);
      expect(retry.body.version).toBe(first.body.version);
      expect(retry.body.eventSeq).toBe(first.body.eventSeq);

      // only one event was persisted
      const events = await dataSource.getRepository(SessionEvent).find({
        where: { sessionId: session.sessionId },
      });
      expect(events).toHaveLength(1);
    });

    it('concurrent duplicate requestIds apply exactly once', async () => {
      const { session } = await setupSession(api);
      const cmd = { requestId: 'start-race', expectedVersion: 0, type: 'start' };
      const [a, b] = await Promise.all([
        api('POST', `/sessions/${session.sessionId}/commands`, {
          token: session.hostToken,
          body: cmd,
        }),
        api('POST', `/sessions/${session.sessionId}/commands`, {
          token: session.hostToken,
          body: cmd,
        }),
      ]);
      expect([a.status, b.status]).toEqual([201, 201]);
      const replayed = [a.body, b.body].filter((r) => r.replayed);
      expect(replayed).toHaveLength(1);
      expect(a.body.version).toBe(b.body.version);
      const state = (
        await api('GET', `/sessions/${session.sessionId}/state`, {
          token: session.hostToken,
        })
      ).body;
      expect(state.version).toBe(1);
    });

    it('rejects stale expectedVersion and reused requestId with new content', async () => {
      const { session } = await setupSession(api);
      await api('POST', `/sessions/${session.sessionId}/commands`, {
        token: session.hostToken,
        body: { requestId: 'r1', expectedVersion: 0, type: 'start' },
      });
      const stale = await api('POST', `/sessions/${session.sessionId}/commands`, {
        token: session.hostToken,
        body: { requestId: 'r2', expectedVersion: 0, type: 'pause' },
      });
      expect(stale.status).toBe(409);

      const reused = await api(
        'POST',
        `/sessions/${session.sessionId}/commands`,
        {
          token: session.hostToken,
          body: { requestId: 'r1', expectedVersion: 1, type: 'pause' },
        },
      );
      expect(reused.status).toBe(409);
    });

    it('enforces the lobby -> live -> paused -> ended lifecycle', async () => {
      const { session } = await setupSession(api);
      const send = (requestId: string, expectedVersion: number, type: string, extra = {}) =>
        api('POST', `/sessions/${session.sessionId}/commands`, {
          token: session.hostToken,
          body: { requestId, expectedVersion, type, ...extra },
        });

      expect((await send('c1', 0, 'pause')).status).toBe(409); // not live yet
      expect((await send('c2', 0, 'start')).status).toBe(201);
      expect((await send('c3', 1, 'goto_scene', { sceneIndex: 2 })).status).toBe(201);
      expect((await send('c4', 2, 'goto_scene', { sceneIndex: 9 })).status).toBe(400);
      expect((await send('c5', 2, 'pause')).status).toBe(201);
      expect((await send('c6', 3, 'resume')).status).toBe(201);
      expect((await send('c7', 4, 'end')).status).toBe(201);
      // ended is terminal
      const after = await send('c8', 5, 'start');
      expect(after.status).toBe(409);
      const state = (
        await api('GET', `/sessions/${session.sessionId}/state`, {
          token: session.hostToken,
        })
      ).body;
      expect(state.status).toBe('ended');
    });
  });

  // ------------------------------------------------------- broadcast & replay

  describe('event log & websocket sync', () => {
    it('persists the event even if the broadcast crashes', async () => {
      const { session } = await setupSession(api);
      const gateway = ctx.app.get(SessionsGateway);
      jest.spyOn(gateway, 'broadcast').mockImplementation(() => {
        throw new Error('simulated crash before broadcast');
      });

      const res = await api('POST', `/sessions/${session.sessionId}/commands`, {
        token: session.hostToken,
        body: { requestId: 'crash-1', expectedVersion: 0, type: 'start' },
      });
      expect(res.status).toBe(201); // command still succeeded

      const events = await dataSource.getRepository(SessionEvent).find({
        where: { sessionId: session.sessionId },
      });
      expect(events).toHaveLength(1);
      expect(events[0].type).toBe('start');

      // a client connecting afterwards replays the event from the log
      jest.restoreAllMocks();
      const guest = (
        await api('POST', '/sessions/join', {
          body: { joinCode: session.joinCode, name: 'g' },
        })
      ).body;
      const ws = new TestWsClient();
      await ws.connect(ctx.port);
      ws.send({
        type: 'subscribe',
        sessionId: session.sessionId,
        token: guest.token,
        lastSeq: 0,
      });
      const snapshot = await ws.waitFor((m) => m.type === 'snapshot');
      expect(snapshot.state.status).toBe('live');
      expect(snapshot.seq).toBe(1);
      ws.close();
    });

    it('replays missed events in order after reconnect', async () => {
      const { session } = await setupSession(api);
      const guest = (
        await api('POST', '/sessions/join', {
          body: { joinCode: session.joinCode, name: 'g' },
        })
      ).body;

      const send = (requestId: string, expectedVersion: number, type: string, extra = {}) =>
        api('POST', `/sessions/${session.sessionId}/commands`, {
          token: session.hostToken,
          body: { requestId, expectedVersion, type, ...extra },
        });
      await send('e1', 0, 'start');
      await send('e2', 1, 'goto_scene', { sceneIndex: 1 });
      await send('e3', 2, 'goto_scene', { sceneIndex: 2 });

      // client was connected up to seq 1, then dropped
      const ws = new TestWsClient();
      await ws.connect(ctx.port);
      ws.send({
        type: 'subscribe',
        sessionId: session.sessionId,
        token: guest.token,
        lastSeq: 1,
      });
      const events = await ws.waitFor(
        (m) => m.type === 'subscribed',
      );
      const replayed = ws.messages
        .filter((m) => m.type === 'event')
        .map((m) => m.seq);
      expect(replayed).toEqual([2, 3]);
      expect(events.seq).toBe(3);
      ws.close();
    });

    it('sends a full snapshot when history is missing or lastSeq is invalid', async () => {
      const { session } = await setupSession(api);
      const guest = (
        await api('POST', '/sessions/join', {
          body: { joinCode: session.joinCode, name: 'g' },
        })
      ).body;
      await api('POST', `/sessions/${session.sessionId}/commands`, {
        token: session.hostToken,
        body: { requestId: 's1', expectedVersion: 0, type: 'start' },
      });

      // lastSeq ahead of the server -> snapshot
      const ws = new TestWsClient();
      await ws.connect(ctx.port);
      ws.send({
        type: 'subscribe',
        sessionId: session.sessionId,
        token: guest.token,
        lastSeq: 9999,
      });
      const snapshot = await ws.waitFor((m) => m.type === 'snapshot');
      expect(snapshot.seq).toBe(1);
      expect(snapshot.state.status).toBe('live');
      ws.close();
    });

    it('broadcasts live events to subscribed clients', async () => {
      const { session } = await setupSession(api);
      const guest = (
        await api('POST', '/sessions/join', {
          body: { joinCode: session.joinCode, name: 'g' },
        })
      ).body;
      const ws = new TestWsClient();
      await ws.connect(ctx.port);
      ws.send({
        type: 'subscribe',
        sessionId: session.sessionId,
        token: guest.token,
        lastSeq: 0,
      });
      await ws.waitFor((m) => m.type === 'subscribed');

      await api('POST', `/sessions/${session.sessionId}/commands`, {
        token: session.hostToken,
        body: { requestId: 'live-1', expectedVersion: 0, type: 'start' },
      });
      const event = await ws.waitFor(
        (m) => m.type === 'event' && m.eventType === 'start',
      );
      expect(event.seq).toBe(1);
      ws.close();
    });

    it('rejects websocket subscribe with a bad token', async () => {
      const { session } = await setupSession(api);
      const ws = new TestWsClient();
      await ws.connect(ctx.port);
      ws.send({
        type: 'subscribe',
        sessionId: session.sessionId,
        token: 'nope',
        lastSeq: 0,
      });
      const err = await ws.waitFor((m) => m.type === 'error');
      expect(err.code).toBe('UNAUTHORIZED');
      ws.close();
    });
  });

  // ------------------------------------------------------------------ timeout

  describe('inactivity timeout', () => {
    async function makeStale(sessionId: string, msAgo: number) {
      await dataSource.getRepository(Session).update(
        { id: sessionId },
        { lastActivityAt: new Date(Date.now() - msAgo) },
      );
    }

    it('ends sessions idle for more than 30 minutes with a persisted event', async () => {
      const { session } = await setupSession(api);
      await makeStale(session.sessionId, 31 * 60 * 1000);

      const sweeper = ctx.app.get(SessionTimeoutService);
      const ended = await sweeper.sweepOnce();
      expect(ended).toEqual([session.sessionId]);

      const row = await dataSource
        .getRepository(Session)
        .findOne({ where: { id: session.sessionId } });
      expect(row.status).toBe('ended');
      expect(row.endedAt).not.toBeNull();

      const events = await dataSource.getRepository(SessionEvent).find({
        where: { sessionId: session.sessionId },
      });
      expect(events).toHaveLength(1);
      expect(events[0].type).toBe('session_ended');
      expect(events[0].payload.reason).toBe('inactivity_timeout');

      // host commands are rejected afterwards
      const res = await api('POST', `/sessions/${session.sessionId}/commands`, {
        token: session.hostToken,
        body: { requestId: 'late', expectedVersion: 0, type: 'start' },
      });
      expect(res.status).toBe(409);
    });

    it('leaves active sessions alone', async () => {
      const { session } = await setupSession(api);
      const sweeper = ctx.app.get(SessionTimeoutService);
      const ended = await sweeper.sweepOnce();
      expect(ended).toEqual([]);
      const row = await dataSource
        .getRepository(Session)
        .findOne({ where: { id: session.sessionId } });
      expect(row.status).toBe('lobby');
    });

    it('resolves the race between the sweeper and a host command consistently', async () => {
      const { session } = await setupSession(api);
      await makeStale(session.sessionId, 31 * 60 * 1000);

      const sweeper = ctx.app.get(SessionTimeoutService);
      const [sweepResult, commandResult] = await Promise.all([
        sweeper.sweepOnce(),
        api('POST', `/sessions/${session.sessionId}/commands`, {
          token: session.hostToken,
          body: { requestId: 'race', expectedVersion: 0, type: 'start' },
        }),
      ]);

      const row = await dataSource
        .getRepository(Session)
        .findOne({ where: { id: session.sessionId } });

      if (commandResult.status === 201) {
        // command won: activity refreshed, sweeper must have skipped
        expect(sweepResult).toEqual([]);
        expect(row.status).toBe('live');
      } else {
        // sweeper won: command rejected, session ended exactly once
        expect(commandResult.status).toBe(409);
        expect(row.status).toBe('ended');
        expect(sweepResult).toEqual([session.sessionId]);
      }
      // either way: no double end, no resurrected session
      const events = await dataSource.getRepository(SessionEvent).find({
        where: { sessionId: session.sessionId, type: 'session_ended' },
      });
      expect(events.length).toBeLessThanOrEqual(1);
    });

    it('unauthorized requests do not refresh the activity timestamp', async () => {
      const { session } = await setupSession(api);
      const stale = new Date(Date.now() - 29 * 60 * 1000);
      await dataSource
        .getRepository(Session)
        .update({ id: session.sessionId }, { lastActivityAt: stale });

      // invalid token
      const bad = await api('POST', `/sessions/${session.sessionId}/commands`, {
        token: 'forged',
        body: { requestId: 'x', expectedVersion: 0, type: 'start' },
      });
      expect(bad.status).toBe(401);

      // valid participant but not host
      const guest = (
        await api('POST', '/sessions/join', {
          body: { joinCode: session.joinCode, name: 'g' },
        })
      ).body;
      const forbidden = await api(
        'POST',
        `/sessions/${session.sessionId}/commands`,
        {
          token: guest.token,
          body: { requestId: 'y', expectedVersion: 0, type: 'start' },
        },
      );
      expect(forbidden.status).toBe(403);

      const row = await dataSource
        .getRepository(Session)
        .findOne({ where: { id: session.sessionId } });
      // join refreshed activity (valid activity), so compare against post-join
      const afterJoin = row.lastActivityAt;

      // another unauthorized attempt must not move the timestamp
      await api('POST', `/sessions/${session.sessionId}/commands`, {
        token: 'forged',
        body: { requestId: 'z', expectedVersion: 0, type: 'start' },
      });
      const row2 = await dataSource
        .getRepository(Session)
        .findOne({ where: { id: session.sessionId } });
      expect(row2.lastActivityAt.getTime()).toBe(afterJoin.getTime());
    });
  });

  // -------------------------------------------------------------- hand raises

  describe('hand raises', () => {
    async function joinGuest(joinCode: string, name: string) {
      return (
        await api('POST', '/sessions/join', { body: { joinCode, name } })
      ).body;
    }

    it('queues in server receive order and dedupes repeats', async () => {
      const { session } = await setupSession(api);
      const g1 = await joinGuest(session.joinCode, 'a');
      const g2 = await joinGuest(session.joinCode, 'b');

      const r1 = await api('POST', `/sessions/${session.sessionId}/hand-raises`, {
        token: g1.token,
      });
      const r2 = await api('POST', `/sessions/${session.sessionId}/hand-raises`, {
        token: g2.token,
      });
      const dup = await api('POST', `/sessions/${session.sessionId}/hand-raises`, {
        token: g1.token,
      });
      expect(r1.body.duplicate).toBe(false);
      expect(r2.body.duplicate).toBe(false);
      expect(dup.body.duplicate).toBe(true);
      expect(dup.body.handRaiseId).toBe(r1.body.handRaiseId);

      const state = (
        await api('GET', `/sessions/${session.sessionId}/state`, {
          token: session.hostToken,
        })
      ).body;
      expect(state.raisedHands.map((h: any) => h.name)).toEqual(['a', 'b']);
    });

    it('participants cancel only their own hand; host resolves', async () => {
      const { session } = await setupSession(api);
      const g1 = await joinGuest(session.joinCode, 'a');
      const g2 = await joinGuest(session.joinCode, 'b');
      await api('POST', `/sessions/${session.sessionId}/hand-raises`, {
        token: g1.token,
      });
      await api('POST', `/sessions/${session.sessionId}/hand-raises`, {
        token: g2.token,
      });

      // g2 cancels their own -> only g1 remains
      const cancel = await api(
        'DELETE',
        `/sessions/${session.sessionId}/hand-raises/mine`,
        { token: g2.token },
      );
      expect(cancel.status).toBe(200);
      let state = (
        await api('GET', `/sessions/${session.sessionId}/state`, {
          token: session.hostToken,
        })
      ).body;
      expect(state.raisedHands.map((h: any) => h.name)).toEqual(['a']);

      // g2 has nothing to cancel anymore
      const again = await api(
        'DELETE',
        `/sessions/${session.sessionId}/hand-raises/mine`,
        { token: g2.token },
      );
      expect(again.status).toBe(404);

      // a participant cannot resolve hands
      const forbidden = await api(
        'POST',
        `/sessions/${session.sessionId}/hand-raises/${g1.participantId}/resolve`,
        { token: g2.token },
      );
      expect(forbidden.status).toBe(403);

      // host resolves g1
      const resolved = await api(
        'POST',
        `/sessions/${session.sessionId}/hand-raises/${g1.participantId}/resolve`,
        { token: session.hostToken },
      );
      expect(resolved.status).toBe(201);
      state = (
        await api('GET', `/sessions/${session.sessionId}/state`, {
          token: session.hostToken,
        })
      ).body;
      expect(state.raisedHands).toEqual([]);
    });
  });
});
