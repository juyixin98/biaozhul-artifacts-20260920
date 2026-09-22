import { MigrationInterface, QueryRunner } from 'typeorm';

export class InitialSchema1727000000000 implements MigrationInterface {
  name = 'InitialSchema1727000000000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`CREATE EXTENSION IF NOT EXISTS "pgcrypto"`);

    await queryRunner.query(`
      CREATE TABLE presentations (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        title       varchar(200) NOT NULL,
        host_id     varchar(200) NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now()
      )
    `);

    await queryRunner.query(`
      CREATE TABLE scenes (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        title            varchar(200) NOT NULL,
        notes            text,
        scene_order      int NOT NULL,
        presentation_id  uuid NOT NULL REFERENCES presentations(id) ON DELETE CASCADE
      )
    `);
    await queryRunner.query(
      `CREATE INDEX idx_scenes_presentation ON scenes (presentation_id, scene_order)`,
    );

    await queryRunner.query(`
      CREATE TABLE presentation_versions (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        presentation_id  uuid NOT NULL REFERENCES presentations(id) ON DELETE RESTRICT,
        version          int NOT NULL,
        scenes           jsonb NOT NULL,
        published_at     timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_presentation_version UNIQUE (presentation_id, version)
      )
    `);

    await queryRunner.query(`
      CREATE TABLE sessions (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        code                char(6) NOT NULL,
        host_id             varchar(200) NOT NULL,
        presentation_id     uuid NOT NULL,
        version_id          uuid NOT NULL,
        version_no          int NOT NULL,
        status              varchar(16) NOT NULL DEFAULT 'lobby',
        current_scene_index int NOT NULL DEFAULT 0,
        max_participants    int NOT NULL DEFAULT 25,
        last_event_seq      bigint NOT NULL DEFAULT 0,
        hand_counter        bigint NOT NULL DEFAULT 0,
        timeout_deadline    timestamptz,
        created_at          timestamptz NOT NULL DEFAULT now(),
        ended_at            timestamptz,
        CONSTRAINT uq_session_code UNIQUE (code),
        CONSTRAINT ck_session_status CHECK (status IN ('lobby','live','paused','ended'))
      )
    `);
    await queryRunner.query(
      `CREATE INDEX idx_session_timeout_deadline ON sessions (timeout_deadline) WHERE timeout_deadline IS NOT NULL`,
    );

    await queryRunner.query(`
      CREATE TABLE participants (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id  uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        user_id     varchar(200) NOT NULL,
        name        varchar(200) NOT NULL,
        role        varchar(16) NOT NULL DEFAULT 'participant',
        joined_at   timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_participant_session_user UNIQUE (session_id, user_id)
      )
    `);
    await queryRunner.query(
      `CREATE INDEX idx_participant_session ON participants (session_id)`,
    );

    await queryRunner.query(`
      CREATE TABLE hand_raises (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id    uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        user_id       varchar(200) NOT NULL,
        name          varchar(200) NOT NULL,
        server_order  bigint NOT NULL,
        raised_at     timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_hand_raise_active UNIQUE (session_id, user_id)
      )
    `);
    await queryRunner.query(
      `CREATE INDEX idx_hand_raise_queue ON hand_raises (session_id, server_order)`,
    );

    await queryRunner.query(`
      CREATE TABLE session_events (
        id          uuid PRIMARY KEY,
        session_id  uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        seq         bigint NOT NULL,
        type        varchar(32) NOT NULL,
        payload     jsonb NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_session_event_seq UNIQUE (session_id, seq)
      )
    `);
    await queryRunner.query(
      `CREATE INDEX idx_session_events_seq ON session_events (session_id, seq)`,
    );

    await queryRunner.query(`
      CREATE TABLE command_records (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id  uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        request_id  varchar(200) NOT NULL,
        command     varchar(64) NOT NULL,
        body_hash   varchar(64) NOT NULL,
        result      jsonb NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now()
      )
    `);
    await queryRunner.query(
      `CREATE UNIQUE INDEX idx_command_session_request ON command_records (session_id, request_id)`,
    );
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DROP TABLE IF EXISTS command_records`);
    await queryRunner.query(`DROP TABLE IF EXISTS session_events`);
    await queryRunner.query(`DROP TABLE IF EXISTS hand_raises`);
    await queryRunner.query(`DROP TABLE IF EXISTS participants`);
    await queryRunner.query(`DROP TABLE IF EXISTS sessions`);
    await queryRunner.query(`DROP TABLE IF EXISTS presentation_versions`);
    await queryRunner.query(`DROP TABLE IF EXISTS scenes`);
    await queryRunner.query(`DROP TABLE IF EXISTS presentations`);
  }
}
