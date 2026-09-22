import {
  Body,
  Controller,
  Get,
  Headers,
  Param,
  ParseIntPipe,
  Post,
  Put,
} from '@nestjs/common';
import { PresentationsService } from './presentations.service';
import { CreatePresentationDto } from './dto/create-presentation.dto';
import { UpdateScenesDto } from './dto/update-scenes.dto';

@Controller('presentations')
export class PresentationsController {
  constructor(private readonly service: PresentationsService) {}

  @Post()
  create(@Body() dto: CreatePresentationDto) {
    return this.service.create(dto);
  }

  @Get(':id')
  get(@Param('id') id: string) {
    return this.service.get(id);
  }

  @Put(':id/scenes')
  update(
    @Param('id') id: string,
    @Body() dto: UpdateScenesDto,
    @Headers('x-user-id') userId = '',
  ) {
    return this.service.updateScenes(id, dto, userId);
  }

  @Post(':id/publish')
  publish(@Param('id') id: string, @Headers('x-user-id') userId = '') {
    return this.service.publish(id, userId);
  }

  @Get(':id/versions/:version')
  getVersion(
    @Param('id') id: string,
    @Param('version', ParseIntPipe) version: number,
  ) {
    return this.service.getVersion(id, version);
  }
}
