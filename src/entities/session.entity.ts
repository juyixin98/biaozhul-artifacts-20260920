import {
  Column,
  CreateDateColumn,
  Entity,
  PrimaryGeneratedColumn,
} from 'typeorm';

export type SessionStatus = 'lobby' | 'live' | 'paused' | 'ended';

export const MAX_PARTICIPANTS = 25;

@Entity('sessions')
export class Session {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'join_code', unique: true, length: 6 })
  joinCode: string;

  /** Bound to an immutable published version; later edits create new versions. */
  @Column({ name: 'presentation_version_id' })
  presentationVersionId: string;

  @Column({ type: 'varchar', default: 'lobby' })
  status: SessionStatus;

  @Column({ name: 'current_scene_index', default: 0 })
  currentSceneIndex: number;

  /** Optimistic-concurrency counter checked against command expectedVersion. */
  @Column({ default: 0 })
  version: number;

  /** Monotonic per-session event sequence, bumped in the same tx as state changes. */
  @Column({ name: 'event_seq', default: 0 })
  eventSeq: number;

  @Column({ name: 'last_activity_at', type: 'timestamptz' })
  lastActivityAt: Date;

  @Column({ name: 'ended_at', type: 'timestamptz', nullable: true })
  endedAt: Date | null;

  @CreateDateColumn({ name: 'created_at', type: 'timestamptz' })
  createdAt: Date;
}
