import { Inject, Injectable, NotFoundException, ForbiddenException } from '@nestjs/common';
import { DataSource } from 'typeorm';
import { randomUUID } from 'crypto';
import { DATA_SOURCE } from '../db/data-source';
import { Presentation, ScenePayload } from './presentation.entity';
import { Scene } from './scene.entity';
import { PresentationVersion } from './presentation-version.entity';
import { CreatePresentationDto } from './dto/create-presentation.dto';
import { UpdateScenesDto } from './dto/update-scenes.dto';

@Injectable()
export class PresentationsService {
  constructor(@Inject(DATA_SOURCE) private readonly ds: DataSource) {}

  async create(dto: CreatePresentationDto) {
    return this.ds.transaction(async (manager) => {
      const presentation = manager.create(Presentation, {
        id: randomUUID(),
        title: dto.title,
        hostId: dto.hostId,
      });
      await manager.save(presentation);

      await manager.save(
        dto.scenes.map((scene, index) =>
          manager.create(Scene, {
            id: randomUUID(),
            title: scene.title,
            notes: scene.notes ?? null,
            order: index,
            presentationId: presentation.id,
          }),
        ),
      );

      return this.serializePresentation(presentation, dto.scenes, []);
    });
  }

  async updateScenes(presentationId: string, dto: UpdateScenesDto, userId: string) {
    return this.ds.transaction(async (manager) => {
      const presentation = await manager.findOneBy(Presentation, { id: presentationId });
      if (!presentation) throw new NotFoundException('Presentation not found');
      if (presentation.hostId !== userId) {
        throw new ForbiddenException('Only the host may edit the draft');
      }

      if (dto.title !== undefined) {
        presentation.title = dto.title;
        await manager.save(presentation);
      }

      // Replace the draft wholesale. Published versions are untouched, so
      // sessions bound to earlier versions keep playing the old snapshot.
      await manager.delete(Scene, { presentationId });
      await manager.save(
        dto.scenes.map((scene, index) =>
          manager.create(Scene, {
            id: randomUUID(),
            title: scene.title,
            notes: scene.notes ?? null,
            order: index,
            presentationId,
          }),
        ),
      );

      const versions = await manager.findBy(PresentationVersion, { presentationId });
      versions.sort((a, b) => a.version - b.version);
      return this.serializePresentation(presentation, dto.scenes, versions);
    });
  }

  async get(presentationId: string) {
    const presentation = await this.ds.manager.findOneBy(Presentation, { id: presentationId });
    if (!presentation) throw new NotFoundException('Presentation not found');

    const scenes = await this.ds.manager.find(Scene, {
      where: { presentationId },
      order: { order: 'ASC' },
    });
    const versions = await this.ds.manager.findBy(PresentationVersion, { presentationId });
    versions.sort((a, b) => a.version - b.version);

    return this.serializePresentation(
      presentation,
      scenes.map((s) => ({ id: s.id, title: s.title, notes: s.notes ?? undefined })),
      versions,
    );
  }

  // Publish takes an immutable ordered snapshot of the current draft. The new
  // version row never changes afterwards.
  async publish(presentationId: string, userId: string) {
    return this.ds.transaction(async (manager) => {
      const presentation = await manager.findOneBy(Presentation, { id: presentationId });
      if (!presentation) throw new NotFoundException('Presentation not found');
      if (presentation.hostId !== userId) {
        throw new ForbiddenException('Only the host may publish');
      }

      const draftScenes = await manager.find(Scene, {
        where: { presentationId },
        order: { order: 'ASC' },
      });
      if (draftScenes.length === 0) {
        throw new NotFoundException('Cannot publish a presentation with no scenes');
      }

      const last = await manager.findOne(PresentationVersion, {
        where: { presentationId },
        order: { version: 'DESC' },
      });
      const nextVersion = (last?.version ?? 0) + 1;

      // Plain objects with fresh ids: draft rows may later be deleted/replaced,
      // the snapshot must not change.
      const snapshot: ScenePayload[] = draftScenes.map((scene) => ({
        id: scene.id,
        title: scene.title,
        ...(scene.notes ? { notes: scene.notes } : {}),
      }));

      const version = manager.create(PresentationVersion, {
        id: randomUUID(),
        presentationId,
        version: nextVersion,
        scenes: snapshot,
      });
      await manager.save(version);

      return {
        id: version.id,
        presentationId,
        version: version.version,
        scenes: snapshot,
        publishedAt: version.publishedAt,
        immutable: true,
      };
    });
  }

  async getVersion(presentationId: string, versionNo: number) {
    const version = await this.ds.manager.findOne(PresentationVersion, {
      where: { presentationId, version: versionNo },
    });
    if (!version) throw new NotFoundException('Version not found');
    return {
      id: version.id,
      presentationId,
      version: version.version,
      scenes: version.scenes,
      publishedAt: version.publishedAt,
      immutable: true,
    };
  }

  private serializePresentation(
    presentation: Presentation,
    draftScenes: Array<{ title: string; notes?: string }>,
    versions: PresentationVersion[],
  ) {
    return {
      id: presentation.id,
      title: presentation.title,
      hostId: presentation.hostId,
      draft: { scenes: draftScenes },
      publishedVersions: versions.map((v) => ({ version: v.version, id: v.id })),
    };
  }
}
