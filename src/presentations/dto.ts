import { Type } from 'class-transformer';
import {
  ArrayMaxSize,
  ArrayMinSize,
  IsArray,
  IsNotEmpty,
  IsOptional,
  IsString,
  ValidateNested,
} from 'class-validator';

export class CreatePresentationDto {
  @IsString()
  @IsNotEmpty()
  title: string;
}

export class SceneDto {
  @IsString()
  @IsNotEmpty()
  title: string;

  @IsOptional()
  @IsString()
  content?: string;
}

export class CreateVersionDto {
  @IsArray()
  @ArrayMinSize(1)
  @ArrayMaxSize(50, { message: 'a presentation supports at most 50 scenes' })
  @ValidateNested({ each: true })
  @Type(() => SceneDto)
  scenes: SceneDto[];
}
