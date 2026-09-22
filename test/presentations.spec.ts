import { INestApplication } from '@nestjs/common';
import request from 'supertest';
import { createTestApp, truncateAll, uniqueUser } from './helpers';

describe('presentation publishing (immutable versions, ordered scenes, 50-scene cap)', () => {
  let app: INestApplication;
  const host = uniqueUser('host');
  const other = uniqueUser('other');

  beforeAll(async () => {
    app = await createTestApp();
    await app.init();
  });
  beforeEach(truncateAll);
  afterAll(() => app.close());

  it('publishes an immutable snapshot; later draft edits do not change version 1', async () => {
    const created = await request(app.getHttpServer())
      .post('/presentations')
      .send({
        title: 'Deck',
        hostId: host.userId,
        scenes: [{ title: 'A' }, { title: 'B' }],
      })
      .expect(201);
    const id = created.body.id;

    const v1 = await request(app.getHttpServer())
      .post(`/presentations/${id}/publish`)
      .set('x-user-id', host.userId)
      .expect(201);
    expect(v1.body.version).toBe(1);
    expect(v1.body.immutable).toBe(true);
    expect(v1.body.scenes.map((s: { title: string }) => s.title)).toEqual(['A', 'B']);

    // Edit the draft after publishing: replace scenes entirely.
    await request(app.getHttpServer())
      .put(`/presentations/${id}/scenes`)
      .set('x-user-id', host.userId)
      .send({ scenes: [{ title: 'C' }] })
      .expect(200);

    const v1Again = await request(app.getHttpServer())
      .get(`/presentations/${id}/versions/1`)
      .expect(200);
    expect(v1Again.body.scenes.map((s: { title: string }) => s.title)).toEqual(['A', 'B']);
  });

  it('publishes sequentially numbered versions and keeps all of them immutable', async () => {
    const created = await request(app.getHttpServer())
      .post('/presentations')
      .send({ title: 'D', hostId: host.userId, scenes: [{ title: 'A' }] })
      .expect(201);
    const id = created.body.id;
    await request(app.getHttpServer()).post(`/presentations/${id}/publish`).set('x-user-id', host.userId).expect(201);
    await request(app.getHttpServer())
      .put(`/presentations/${id}/scenes`)
      .set('x-user-id', host.userId)
      .send({ scenes: [{ title: 'X' }, { title: 'Y' }] })
      .expect(200);
    const v2 = await request(app.getHttpServer()).post(`/presentations/${id}/publish`).set('x-user-id', host.userId).expect(201);
    expect(v2.body.version).toBe(2);

    const v1 = await request(app.getHttpServer()).get(`/presentations/${id}/versions/1`).expect(200);
    expect(v1.body.scenes.map((s: { title: string }) => s.title)).toEqual(['A']);
  });

  it('rejects more than 50 scenes', async () => {
    await request(app.getHttpServer())
      .post('/presentations')
      .send({
        title: 'Too many',
        hostId: host.userId,
        scenes: Array.from({ length: 51 }, (_, i) => ({ title: `S${i}` })),
      })
      .expect(400);
  });

  it('forbids non-owners from editing or publishing', async () => {
    const created = await request(app.getHttpServer())
      .post('/presentations')
      .send({ title: 'D', hostId: host.userId, scenes: [{ title: 'A' }] })
      .expect(201);
    const id = created.body.id;

    await request(app.getHttpServer())
      .put(`/presentations/${id}/scenes`)
      .set('x-user-id', other.userId)
      .send({ scenes: [{ title: 'Z' }] })
      .expect(403);
    await request(app.getHttpServer())
      .post(`/presentations/${id}/publish`)
      .set('x-user-id', other.userId)
      .expect(403);
  });
});
