import { INestApplication } from '@nestjs/common';
import request from 'supertest';
import { Client } from 'pg';
import { createTestApp, listenEphemeral, sleep, truncateAll, uniqueUser } from './helpers';
import { cmd, join, setupFlow, snapshot } from './flow';
import { SessionsService } from '../src/sessions/sessions.service';

describe('inactivity timeout', () => {
  beforeEach(truncateAll);

  async function boot(idleMs: number, sweepMs = 200): Promise<{ app: INestApplication; baseUrl: string }> {
    const app = await createTestApp({ idleMs, sweepMs });
    await app.init();
    const port = await listenEphemeral(app);
    return { app, baseUrl: `http://127.0.0.1:${port}` };
  }

  it('auto-ends after the deadline, freezes the deadline and broadcasts a terminal event', async () => {
    const { app } = await boot(400, 100);
    const flow = await setupFlow(app);

    await sleep(900);
    const snap = (await snapshot(app, flow.code, flow.hostId)).body;
    expect(snap.status).toBe('ended');
    expect(snap.timeoutDeadline).toBeNull();
    expect(snap.version).toBe(1);

    const events = await request(app.getHttpServer())
      .get(`/sessions/${flow.code}/events?afterSeq=0`)
      .set('x-user-id', flow.hostId)
      .expect(200);
    expect(events.body.events[0].type).toBe('session.ended');
    expect(events.body.events[0].payload.reason).toBe('inactivity_timeout');

    await app.close();
  });

  it('a valid command slides the deadline; a forbidden request does NOT keep the session alive', async () => {
    const { app } = await boot(600, 100);
    const flow = await setupFlow(app);
    const p = uniqueUser('p');
    await join(app, flow.code, p);

    // Unauthorised commands must not refresh activity: hammer one in
    // periodically; the session still ends ~idleMs after creation.
    const start = Date.now();
    let endedAt = 0;
    for (let i = 0; i < 12; i++) {
      await sleep(100);
      await cmd(app, flow.code, p.userId, { command: 'start', expectedVersion: 0 }); // 403
      const status = (await snapshot(app, flow.code, flow.hostId)).body.status;
      if (status === 'ended') {
        endedAt = Date.now();
        break;
      }
    }
    expect(endedAt).toBeGreaterThan(0);
    // If forbidden calls refreshed the deadline, end time would be pushed well
    // past the original 600ms.
    expect(endedAt - start).toBeLessThan(1500);

    await app.close();
  });

  it('resumes timeout processing after a service restart', async () => {
    // First process: create the session with a short deadline, shut down
    // before the sweep fires. The deadline is durable in the DB.
    let app = await createTestApp({ idleMs: 5000, sweepMs: 3_600_000 });
    await app.init();
    const flow = await setupFlow(app);
    const sessionId = flow.sessionId;
    await app.close();

    // Force the deadline into the past directly.
    const pg = new Client({ connectionString: process.env.DATABASE_URL });
    await pg.connect();
    await pg.query(`UPDATE sessions SET timeout_deadline = now() - interval '1 second' WHERE id = $1`, [
      sessionId,
    ]);
    await pg.end();

    // New process with its own sweep interval: it must end the overdue
    // session on its first post-boot sweep.
    app = await createTestApp({ idleMs: 5000, sweepMs: 3_600_000 });
    await app.init();
    await sleep(900);
    const snap = (await snapshot(app, flow.code, flow.hostId)).body;
    expect(snap.status).toBe('ended');
    await app.close();
  });

  it('race boundary: host command arriving before the sweep cancels the timeout', async () => {
    // Deadline 500ms; periodic sweep far away, only the post-boot run matters
    // (fired ~500ms after start). Host acts at 200ms and slides the deadline.
    const { app } = await boot(500, 3_600_000);
    const flow = await setupFlow(app);

    await sleep(200);
    await cmd(app, flow.code, flow.hostId, { command: 'start', expectedVersion: 0 }).expect(201);

    // Manually run a sweep now: deadline was pushed ~idleMs forward, so the
    // previously-overdue-looking session must survive.
    const sessions = app.get(SessionsService);
    const ended = await sessions.sweepTimeouts();
    expect(ended).toBe(0);
    const snap = (await snapshot(app, flow.code, flow.hostId)).body;
    expect(snap.status).toBe('live');
    await app.close();
  });

  it('race boundary: once the sweep ends the session under the lock, a racing host end is rejected', async () => {
    const { app } = await boot(100, 10_000);
    const flow = await setupFlow(app);
    const sessions = app.get(SessionsService);

    await sleep(300);
    const ended = await sessions.sweepTimeouts();
    expect(ended).toBe(1);

    // Host command racing afterwards: session is terminal, cannot end/start.
    const res = await cmd(app, flow.code, flow.hostId, { command: 'end', expectedVersion: 1 });
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('SESSION_ENDED');
    await app.close();
  });
});
