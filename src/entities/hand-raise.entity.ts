import {
  Column,
  CreateDateColumn,
  Entity,
  PrimaryGeneratedColumn,
} from 'typeorm';

export type HandRaiseStatus = 'raised' | 'resolved' | 'cancelled';

/**
 * Identity PK gives the server-side receive order, which is the queue order.
 * A partial unique index on (session_id, participant_id) WHERE status='raised'
 * makes duplicate raise requests idempotent.
 */
@Entity('hand_raises')
export class HandRaise {
  @PrimaryGeneratedColumn()
  id: number;

  @Column({ name: 'session_id' })
  sessionId: string;

  @Column({ name: 'participant_id' })
  participantId: string;

  @Column({ type: 'varchar', default: 'raised' })
  status: HandRaiseStatus;

  @CreateDateColumn({ name: 'created_at', type: 'timestamptz' })
  createdAt: Date;

  @Column({ name: 'handled_at', type: 'timestamptz', nullable: true })
  handledAt: Date | null;
}
