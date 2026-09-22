import {
  Column,
  CreateDateColumn,
  Entity,
  Index,
  PrimaryColumn,
} from 'typeorm';

// Idempotency record for client commands. Same requestId always returns the
// original stored result; a retry with a different body is a conflict.
@Entity('command_records')
@Index('idx_command_session_request', ['sessionId', 'requestId'], { unique: true })
export class CommandRecord {
  @PrimaryColumn({ type: 'uuid' })
  id: string;

  @Column({ type: 'uuid', name: 'session_id' })
  sessionId: string;

  @Column({ type: 'varchar', length: 200, name: 'request_id' })
  requestId: string;

  @Column({ type: 'varchar', length: 64 })
  command: string;

  // Hash of the request body used to detect conflicting retries.
  @Column({ type: 'varchar', length: 64, name: 'body_hash' })
  bodyHash: string;

  // Serialised original response.
  @Column({ type: 'jsonb' })
  result: Record<string, unknown>;

  @CreateDateColumn({ type: 'timestamptz', name: 'created_at' })
  createdAt: Date;
}
