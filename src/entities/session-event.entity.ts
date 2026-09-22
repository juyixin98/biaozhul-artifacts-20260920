import {
  Column,
  CreateDateColumn,
  Entity,
  PrimaryGeneratedColumn,
} from 'typeorm';

/**
 * Durable event log. Every state change inserts a row here with the next
 * per-session seq inside the same transaction, so a crash after commit but
 * before broadcast never loses an event: reconnecting clients replay from
 * this table.
 */
@Entity('session_events')
export class SessionEvent {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'session_id' })
  sessionId: string;

  @Column()
  seq: number;

  @Column()
  type: string;

  @Column({ type: 'jsonb' })
  payload: Record<string, unknown>;

  @CreateDateColumn({ name: 'created_at', type: 'timestamptz' })
  createdAt: Date;
}
