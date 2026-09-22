import 'reflect-metadata';
import { DataSource } from 'typeorm';
import { AppConfig } from '../config/app-config';
import { InitialSchema1727000000000 } from '../migrations/1727000000000-initial-schema';
import { Presentation } from '../presentations/presentation.entity';
import { Scene } from '../presentations/scene.entity';
import { PresentationVersion } from '../presentations/presentation-version.entity';
import { Session } from '../sessions/session.entity';
import { Participant } from '../sessions/participant.entity';
import { HandRaise } from '../sessions/hand-raise.entity';
import { SessionEvent } from '../sessions/session-event.entity';
import { CommandRecord } from '../sessions/command-record.entity';

export const entities = [
  Presentation,
  Scene,
  PresentationVersion,
  Session,
  Participant,
  HandRaise,
  SessionEvent,
  CommandRecord,
];

export const migrations = [InitialSchema1727000000000];

export function createDataSource(config: AppConfig): DataSource {
  return new DataSource({
    type: 'postgres',
    url: config.databaseUrl,
    entities,
    migrations,
    synchronize: false,
    extra: { max: 10 },
  });
}

export const DATA_SOURCE = 'DATA_SOURCE';
