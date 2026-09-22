import { INestApplication } from '@nestjs/common';
import request from 'supertest';
import { createTestApp, truncateAll, uniqueUser } from './helpers';
import { join, setupFlow } from './flow';

describe('join-code capacity under concurrency', () => {
  let app: INestApplication;

  beforeAll(async () => {
    app = await createTestApp();
    await app.init();
  });
  beforeEach(truncateAll);
  afterAll(() => app.close());

  it('generates a unique 6-character code', async () => {
    const flow = await setupFlow(app);
    expect(flow.code).toMatch(/^[A-HJ-NP-Z2-9]{6}$/);
  });

  it('admits at most 24 more participants (25 seats incl. host); concurrent joins never overflow', async () => {
    const flow = await setupFlow(app);

    // Fire 30 joins simultaneously: 24 must succeed, the rest get SESSION_FULL.
    const users = Array.from({ length: 30 }, () => uniqueUser('p'));
    const results = await Promise.all(
      users.map((user) =>
        request(app.getHttpServer())
          .post(`/sessions/${flow.code}/join`)
          .send(user)
          .then((res) => res.status)
          .catch(() => 0),
      ),
    );

    const accepted = results.filter((s) => s === 201).length;
    const full = results.filter((s) => s === 409).length;
    expect(accepted).toBe(24);
    expect(full).toBe(6);

    const snap = await request(app.getHttpServer())
      .get(`/sessions/${flow.code}/snapshot`)
      .set('x-user-id', flow.hostId)
      .expect(200);
    expect(snap.body.participants).toHaveLength(25);
  });

  it('rejoin by the same user is idempotent and does not consume another seat', async () => {
    const flow = await setupFlow(app);
    const user = uniqueUser('p');
    await join(app, flow.code, user).expect(201);
    await join(app, flow.code, user).expect(201);

    const snap = await request(app.getHttpServer())
      .get(`/sessions/${flow.code}/snapshot`)
      .set('x-user-id', flow.hostId)
      .expect(200);
    expect(snap.body.participants).toHaveLength(2);
  });
});
