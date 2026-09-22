import { MigrationInterface, QueryRunner } from 'typeorm';

export class Init1700000000000 implements MigrationInterface {
  name = 'Init1700000000000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE presentations (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        title varchar NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
      )
    `);

    await queryRunner.query(`
      CREATE TABLE presentation_versions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        presentation_id uuid NOT NULL REFERENCES presentations(id) ON DELETE CASCADE,
        version_number integer NOT NULL,
        scenes jsonb NOT NULL,
        published boolean NOT NULL DEFAULT false,
        published_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_presentation_version UNIQUE (presentation_id, version_number)
      )
    `);

    await queryRunner.query(`
      CREATE TABLE sessions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        join_code varchar(6) NOT NULL UNIQUE,
        presentation_version_id uuid NOT NULL REFERENCES presentation_versions(id),
        status varchar NOT NULL DEFAULT 'lobby',
        current_scene_index integer NOT NULL DEFAULT 0,
        version integer NOT NULL DEFAULT 0,
        event_seq integer NOT NULL DEFAULT 0,
        last_activity_at timestamptz NOT NULL,
        ended_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
      )
    `);
    await queryRunner.query(
      `CREATE INDEX idx_sessions_activity ON sessions (status, last_activity_at)`,
    );

    await queryRunner.query(`
      CREATE TABLE participants (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        name varchar NOT NULL,
        role varchar NOT NULL DEFAULT 'participant',
        token varchar NOT NULL UNIQUE,
        joined_at timestamptz NOT NULL DEFAULT now()
      )
    `);
    await queryRunner.query(
      `CREATE INDEX idx_participants_session ON participants (session_id)`,
    );

    await queryRunner.query(`
      CREATE TABLE session_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        seq integer NOT NULL,
        type varchar NOT NULL,
        payload jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_session_event_seq UNIQUE (session_id, seq)
      )
    `);
    await queryRunner.query(
      `CREATE INDEX idx_session_events_lookup ON session_events (session_id, seq)`,
    );

    await queryRunner.query(`
      CREATE TABLE command_receipts (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        request_id varchar NOT NULL,
        command_type varchar NOT NULL,
        request_hash varchar NOT NULL,
        result jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_command_request UNIQUE (session_id, request_id)
      )
    `);

    await queryRunner.query(`
      CREATE TABLE hand_raises (
        id BIGSERIAL PRIMARY KEY,
        session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        participant_id uuid NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
        status varchar NOT NULL DEFAULT 'raised',
        created_at timestamptz NOT NULL DEFAULT now(),
        handled_at timestamptz
      )
    `);
    // One active raised hand per participant per session: makes duplicate
    // raise requests idempotent at the database level.
    await queryRunner.query(`
      CREATE UNIQUE INDEX uq_active_hand_raise
        ON hand_raises (session_id, participant_id)
        WHERE status = 'raised'
    `);
    await queryRunner.query(
      `CREATE INDEX idx_hand_raises_queue ON hand_raises (session_id, id) WHERE status = 'raised'`,
    );
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DROP TABLE IF EXISTS hand_raises`);
    await queryRunner.query(`DROP TABLE IF EXISTS command_receipts`);
    await queryRunner.query(`DROP TABLE IF EXISTS session_events`);
    await queryRunner.query(`DROP TABLE IF EXISTS participants`);
    await queryRunner.query(`DROP TABLE IF EXISTS sessions`);
    await queryRunner.query(`DROP TABLE IF EXISTS presentation_versions`);
    await queryRunner.query(`DROP TABLE IF EXISTS presentations`);
  }
}
