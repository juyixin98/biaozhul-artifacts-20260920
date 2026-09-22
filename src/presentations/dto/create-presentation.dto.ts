import {
  ArrayMaxSize,
  IsArray,
  IsOptional,
  IsString,
  Length,
  MaxLength,
  ValidateNested,
} from 'class-validator';
import { Type } from 'class-transformer';

export class SceneDraftDto {
  @IsString()
  @Length(1, 200)
  title: string;

  @IsOptional()
  @IsString()
  @MaxLength(5000)
  notes?: string;
}

export class CreatePresentationDto {
  @IsString()
  @Length(1, 200)
  title: string;

  @IsString()
  @Length(1, 200)
  hostId: string;

  @IsArray()
  @ArrayMaxSize(50)
  @ValidateNested({ each: true })
  @Type(() => SceneDraftDto)
  scenes: SceneDraftDto[];
}
