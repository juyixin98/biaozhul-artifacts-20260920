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

export const ROLE_HOST = 'host';
export const ROLE_PARTICIPANT = 'participant';

@Entity('participants')
// A user joins a session at most once; the unique index also serialises
// concurrent joins so the 25-seat cap can never be exceeded.
@Unique('uq_participant_session_user', ['sessionId', 'userId'])
@Index('idx_participant_session', ['sessionId'])
export class Participant {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ type: 'uuid', name: 'session_id' })
  sessionId: string;

  @Column({ type: 'varchar', length: 200, name: 'user_id' })
  userId: string;

  @Column({ type: 'varchar', length: 200 })
  name: string;

  @Column({ type: 'varchar', length: 16, default: ROLE_PARTICIPANT })
  role: typeof ROLE_HOST | typeof ROLE_PARTICIPANT;

  @CreateDateColumn({ type: 'timestamptz', name: 'joined_at' })
  joinedAt: Date;

  @ManyToOne(() => Session, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'session_id' })
  session: Session;
}
