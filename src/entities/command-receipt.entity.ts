import {
  Column,
  CreateDateColumn,
  Entity,
  PrimaryGeneratedColumn,
} from 'typeorm';

/**
 * Idempotency record for host commands, keyed by (session_id, request_id).
 * A retry with the same content returns the stored result without
 * re-applying the command.
 */
@Entity('command_receipts')
export class CommandReceipt {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'session_id' })
  sessionId: string;

  @Column({ name: 'request_id' })
  requestId: string;

  @Column({ name: 'command_type' })
  commandType: string;

  /** Canonical JSON of the command payload, to detect requestId reuse. */
  @Column({ name: 'request_hash' })
  requestHash: string;

  @Column({ type: 'jsonb' })
  result: Record<string, unknown>;

  @CreateDateColumn({ name: 'created_at', type: 'timestamptz' })
  createdAt: Date;
}
