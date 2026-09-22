import {
  Column,
  CreateDateColumn,
  Entity,
  PrimaryGeneratedColumn,
} from 'typeorm';

export interface Scene {
  title: string;
  content?: string;
}

@Entity('presentation_versions')
export class PresentationVersion {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'presentation_id' })
  presentationId: string;

  @Column({ name: 'version_number' })
  versionNumber: number;

  /** Ordered scene list, max 50 entries. Frozen once published. */
  @Column({ type: 'jsonb' })
  scenes: Scene[];

  @Column({ default: false })
  published: boolean;

  @Column({ name: 'published_at', type: 'timestamptz', nullable: true })
  publishedAt: Date | null;

  @CreateDateColumn({ name: 'created_at', type: 'timestamptz' })
  createdAt: Date;
}
