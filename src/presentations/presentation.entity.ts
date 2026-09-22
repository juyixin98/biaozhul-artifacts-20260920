import {
  Column,
  CreateDateColumn,
  Entity,
  OneToMany,
  PrimaryGeneratedColumn,
} from 'typeorm';
import { Scene } from './scene.entity';
import { PresentationVersion } from './presentation-version.entity';

export interface ScenePayload {
  id: string;
  title: string;
  notes?: string;
}

@Entity('presentations')
export class Presentation {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ type: 'varchar', length: 200 })
  title: string;

  @Column({ type: 'varchar', length: 200, name: 'host_id' })
  hostId: string;

  @CreateDateColumn({ type: 'timestamptz', name: 'created_at' })
  createdAt: Date;

  @OneToMany(() => Scene, (scene) => scene.presentation)
  scenes: Scene[];

  @OneToMany(() => PresentationVersion, (version) => version.presentation)
  versions: PresentationVersion[];
}
