import {
  Column,
  CreateDateColumn,
  Entity,
  Index,
  JoinColumn,
  ManyToOne,
  PrimaryGeneratedColumn,
  Unique,
} from 'typeorm';
import { Session } from './session.entity';

// Hand-raise queue. server_order is a per-session monotonic counter so the
// queue follows server-side reception order.
@Entity('hand_raises')
@Unique('uq_hand_raise_active', ['sessionId', 'userId'])
@Index('idx_hand_raise_queue', ['sessionId', 'serverOrder'])
export class HandRaise {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ type: 'uuid', name: 'session_id' })
  sessionId: string;

  @Column({ type: 'varchar', length: 200, name: 'user_id' })
  userId: string;

  @Column({ type: 'varchar', length: 200 })
  name: string;

  @Column({ type: 'bigint', name: 'server_order' })
  serverOrder: string;

  @CreateDateColumn({ type: 'timestamptz', name: 'raised_at' })
  raisedAt: Date;

  @ManyToOne(() => Session, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'session_id' })
  session: Session;
}
