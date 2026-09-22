import {
  Column,
  CreateDateColumn,
  Entity,
  Index,
  PrimaryColumn,
  Unique,
} from 'typeorm';

export type SessionEventType =
  | 'session.started'
  | 'session.paused'
  | 'session.resumed'
  | 'session.ended'
  | 'scene.changed'
  | 'hand.raised'
  | 'hand.cancelled'
  | 'hand.handled';

@Entity('session_events')
// Per-session gapless sequence. The unique index rejects duplicate/out-of-order
// sequence numbers so the state can never roll backwards.
@Unique('uq_session_event_seq', ['sessionId', 'seq'])
@Index('idx_session_events_seq', ['sessionId', 'seq'])
export class SessionEvent {
  // Random id; clients track progress via (sessionId, seq), not this column.
  @PrimaryColumn({ type: 'uuid' })
  id: string;

  @Column({ type: 'uuid', name: 'session_id' })
  sessionId: string;

  @Column({ type: 'bigint' })
  seq: string;

  @Column({ type: 'varchar', length: 32 })
  type: string;

  // Full event payload, also containing the resulting snapshot fields.
  @Column({ type: 'jsonb' })
  payload: Record<string, unknown>;

  @CreateDateColumn({ type: 'timestamptz', name: 'created_at' })
  createdAt: Date;
}
