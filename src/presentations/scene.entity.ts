import {
  Column,
  Entity,
  JoinColumn,
  ManyToOne,
  PrimaryGeneratedColumn,
} from 'typeorm';
import { Presentation } from './presentation.entity';

// Draft scene. Publishing snapshots the ordered draft into an immutable
// presentation_version row; later edits never touch a published version.
@Entity('scenes')
export class Scene {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ type: 'varchar', length: 200 })
  title: string;

  @Column({ type: 'text', nullable: true })
  notes: string | null;

  @Column({ type: 'int', name: 'scene_order' })
  order: number;

  @Column({ type: 'uuid', name: 'presentation_id' })
  presentationId: string;

  @ManyToOne(() => Presentation, (presentation) => presentation.scenes, {
    onDelete: 'CASCADE',
  })
  @JoinColumn({ name: 'presentation_id' })
  presentation: Presentation;
}
