import { Type } from 'class-transformer';
import {
  ArrayMaxSize,
  IsArray,
  IsOptional,
  IsString,
  Length,
  MaxLength,
  ValidateNested,
} from 'class-validator';
import { SceneDraftDto } from './create-presentation.dto';

// Draft edits replace the whole ordered draft scene list. Up to 50 scenes.
// Published versions are never touched by edits.
export class UpdateScenesDto {
  @IsOptional()
  @IsString()
  @Length(1, 200)
  title?: string;

  @IsArray()
  @ArrayMaxSize(50)
  @ValidateNested({ each: true })
  @Type(() => SceneDraftDto)
  scenes: SceneDraftDto[];
}
