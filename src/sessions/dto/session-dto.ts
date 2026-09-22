import { IsInt, IsString, Length, Max, Min } from 'class-validator';
import { Type } from 'class-transformer';

export class CreateSessionDto {
  @IsString()
  @Length(1, 200)
  hostId: string;

  @IsString()
  @Length(1, 200)
  hostName: string;

  @IsString()
  presentationId: string;

  @Type(() => Number)
  @IsInt()
  @Min(1)
  version: number;
}

export class JoinSessionDto {
  @IsString()
  @Length(1, 200)
  userId: string;

  @IsString()
  @Length(1, 200)
  name: string;
}
