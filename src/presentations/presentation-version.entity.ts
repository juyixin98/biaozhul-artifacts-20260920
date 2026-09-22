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
import { Presentation, ScenePayload } from './presentation.entity';

@Entity('presentation_versions')
@Unique('uq_presentation_version', ['presentationId', 'version'])
export class PresentationVersion {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ type: 'uuid', name: 'presentation_id' })
  presentationId: string;

  @Column({ type: 'int' })
  version: number;

  // Immutable ordered snapshot taken at publish time.
  @Column({ type: 'jsonb' })
  scenes: ScenePayload[];

  @CreateDateColumn({ type: 'timestamptz', name: 'published_at' })
  publishedAt: Date;

  @ManyToOne(() => Presentation, (presentation) => presentation.versions, {
    onDelete: 'RESTRICT',
  })
  @JoinColumn({ name: 'presentation_id' })
  presentation: Presentation;
}
