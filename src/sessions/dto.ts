import {
  IsIn,
  IsInt,
  IsNotEmpty,
  IsOptional,
  IsString,
  IsUUID,
  Min,
} from 'class-validator';

export const COMMAND_TYPES = [
  'start',
  'pause',
  'resume',
  'goto_scene',
  'end',
] as const;
export type CommandType = (typeof COMMAND_TYPES)[number];

export class CreateSessionDto {
  @IsUUID()
  presentationVersionId: string;

  @IsString()
  @IsNotEmpty()
  hostName: string;
}

export class JoinSessionDto {
  @IsString()
  @IsNotEmpty()
  joinCode: string;

  @IsString()
  @IsNotEmpty()
  name: string;
}

export class CommandDto {
  /** Client-generated idempotency key. Same content + same id => original result. */
  @IsString()
  @IsNotEmpty()
  requestId: string;

  /** Optimistic concurrency: must equal the session's current version. */
  @IsInt()
  @Min(0)
  expectedVersion: number;

  @IsIn(COMMAND_TYPES as unknown as string[])
  type: CommandType;

  @IsOptional()
  @IsInt()
  @Min(0)
  sceneIndex?: number;
}
