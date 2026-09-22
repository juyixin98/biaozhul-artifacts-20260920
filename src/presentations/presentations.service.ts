import {
  ConflictException,
  Injectable,
  NotFoundException,
} from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Presentation } from '../entities/presentation.entity';
import { PresentationVersion } from '../entities/presentation-version.entity';
import { CreatePresentationDto, CreateVersionDto } from './dto';

@Injectable()
export class PresentationsService {
  constructor(
    @InjectRepository(Presentation)
    private readonly presentations: Repository<Presentation>,
    @InjectRepository(PresentationVersion)
    private readonly versions: Repository<PresentationVersion>,
  ) {}

  async createPresentation(dto: CreatePresentationDto) {
    const p = await this.presentations.save(
      this.presentations.create({ title: dto.title }),
    );
    return { id: p.id, title: p.title, createdAt: p.createdAt };
  }

  async createVersion(presentationId: string, dto: CreateVersionDto) {
    const presentation = await this.presentations.findOne({
      where: { id: presentationId },
    });
    if (!presentation) throw new NotFoundException('presentation not found');
    const count = await this.versions.count({ where: { presentationId } });
    const version = await this.versions.save(
      this.versions.create({
        presentationId,
        versionNumber: count + 1,
        scenes: dto.scenes,
        published: false,
      }),
    );
    return this.toDto(version);
  }

  /**
   * Publishing freezes the version: there is intentionally no update/delete
   * endpoint for versions, and publish refuses to run twice.
   */
  async publish(versionId: string) {
    const version = await this.versions.findOne({ where: { id: versionId } });
    if (!version) throw new NotFoundException('version not found');
    if (version.published) {
      throw new ConflictException({
        code: 'ALREADY_PUBLISHED',
        message: 'published versions are immutable',
      });
    }
    version.published = true;
    version.publishedAt = new Date();
    return this.toDto(await this.versions.save(version));
  }

  async getVersion(versionId: string) {
    const version = await this.versions.findOne({ where: { id: versionId } });
    if (!version) throw new NotFoundException('version not found');
    return this.toDto(version);
  }

  private toDto(v: PresentationVersion) {
    return {
      id: v.id,
      presentationId: v.presentationId,
      versionNumber: v.versionNumber,
      scenes: v.scenes,
      published: v.published,
      publishedAt: v.publishedAt,
    };
  }
}
