import { INestApplication } from '@nestjs/common';
import request from 'supertest';
import { uniqueUser } from './helpers';

export interface Flow {
  presentationId: string;
  versionNo: number;
  versionId: string;
  sessionId: string;
  code: string;
  hostId: string;
  hostName: string;
  scenes: Array<{ id: string; title: string }>;
}

type HttpServer = ReturnType<INestApplication['getHttpServer']>;

// Creates a presentation with `sceneCount` scenes, publishes version 1 and
// opens a session. Returns all ids tests need.
export async function setupFlow(
  app: INestApplication,
  sceneCount = 5,
  host = uniqueUser('host'),
): Promise<Flow> {
  const created = await request(app.getHttpServer())
    .post('/presentations')
    .send({
      title: 'Deck',
      hostId: host.userId,
      scenes: Array.from({ length: sceneCount }, (_, i) => ({
        title: `Scene ${i + 1}`,
        notes: i === 0 ? 'intro notes' : undefined,
      })),
    })
    .expect(201);

  const presentationId = created.body.id;
  const published = await request(app.getHttpServer())
    .post(`/presentations/${presentationId}/publish`)
    .set('x-user-id', host.userId)
    .expect(201);

  const session = await request(app.getHttpServer())
    .post('/sessions')
    .send({
      hostId: host.userId,
      hostName: host.name,
      presentationId,
      version: 1,
    })
    .expect(201);

  return {
    presentationId,
    versionNo: published.body.version,
    versionId: published.body.id,
    sessionId: session.body.id,
    code: session.body.code,
    hostId: host.userId,
    hostName: host.name,
    scenes: published.body.scenes,
  };
}

export function join(
  app: INestApplication,
  code: string,
  user: { userId: string; name: string },
) {
  return request(app.getHttpServer())
    .post(`/sessions/${code}/join`)
    .send(user);
}

export function cmd(
  app: INestApplication,
  code: string,
  userId: string,
  body: {
    command: string;
    requestId?: string;
    expectedVersion: number;
    sceneIndex?: number;
    name?: string;
    targetUserId?: string;
  },
) {
  return request(app.getHttpServer() as HttpServer)
    .post(`/sessions/${code}/commands`)
    .set('x-user-id', userId)
    .send({ requestId: `req-${Math.random().toString(36).slice(2)}`, ...body });
}

export function snapshot(app: INestApplication, code: string, userId: string) {
  return request(app.getHttpServer())
    .get(`/sessions/${code}/snapshot`)
    .set('x-user-id', userId);
}

