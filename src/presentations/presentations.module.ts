import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Presentation } from '../entities/presentation.entity';
import { PresentationVersion } from '../entities/presentation-version.entity';
import { PresentationsController } from './presentations.controller';
import { PresentationsService } from './presentations.service';

@Module({
  imports: [TypeOrmModule.forFeature([Presentation, PresentationVersion])],
  controllers: [PresentationsController],
  providers: [PresentationsService],
})
export class PresentationsModule {}
