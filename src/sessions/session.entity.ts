import {
  Column,
  CreateDateColumn,
  Entity,
  Index,
  PrimaryGeneratedColumn,
} from 'typeorm';

export type SessionStatus = 'lobby' | 'live' | 'paused' | 'ended';

@Entity('sessions')
export class Session {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  // Unique 6-char join code.
  @Index('uq_session_code', { unique: true })
  @Column({ type: 'char', length: 6 })
  code: string;

  @Column({ type: 'varchar', length: 200, name: 'host_id' })
  hostId: string;

  @Column({ type: 'uuid', name: 'presentation_id' })
  presentationId: string;

  // The session binds to one immutable published version.
  @Column({ type: 'uuid', name: 'version_id' })
  versionId: string;

  @Column({ type: 'int', name: 'version_no' })
  versionNo: number;

  @Column({ type: 'varchar', length: 16, default: 'lobby' })
  status: SessionStatus;

  @Column({ type: 'int', name: 'current_scene_index', default: 0 })
  currentSceneIndex: number;

  @Column({ type: 'int', default: 25, name: 'max_participants' })
  maxParticipants: number;

  // Monotonic per-session event sequence.
  @Column({ type: 'bigint', name: 'last_event_seq', default: 0 })
  lastEventSeq: string;

  // Monotonic counter for hand-raise queue positions. Never reused, even after
  // a hand is handled, so queue order is stable.
  @Column({ type: 'bigint', name: 'hand_counter', default: 0 })
  handCounter: string;

  // Deadline for the inactivity auto-end. NULL after the session ends.
  // Durable so timeout processing resumes after a restart.
  @Index('idx_session_timeout_deadline')
  @Column({ type: 'timestamptz', name: 'timeout_deadline', nullable: true })
  timeoutDeadline: Date | null;

  @CreateDateColumn({ type: 'timestamptz', name: 'created_at' })
  createdAt: Date;

  @Column({ type: 'timestamptz', name: 'ended_at', nullable: true })
  endedAt: Date | null;
}
