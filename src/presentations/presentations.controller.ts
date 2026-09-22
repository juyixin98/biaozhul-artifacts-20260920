import { Body, Controller, Get, Param, Post } from '@nestjs/common';
import { CreatePresentationDto, CreateVersionDto } from './dto';
import { PresentationsService } from './presentations.service';

@Controller()
export class PresentationsController {
  constructor(private readonly service: PresentationsService) {}

  @Post('presentations')
  createPresentation(@Body() dto: CreatePresentationDto) {
    return this.service.createPresentation(dto);
  }

  @Post('presentations/:id/versions')
  createVersion(@Param('id') id: string, @Body() dto: CreateVersionDto) {
    return this.service.createVersion(id, dto);
  }

  @Post('presentation-versions/:id/publish')
  publish(@Param('id') id: string) {
    return this.service.publish(id);
  }

  @Get('presentation-versions/:id')
  getVersion(@Param('id') id: string) {
    return this.service.getVersion(id);
  }
}
