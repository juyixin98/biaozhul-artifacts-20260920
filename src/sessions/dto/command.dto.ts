import { IsInt, IsOptional, IsString, Max, MaxLength, Min } from 'class-validator';
import { Type } from 'class-transformer';

// Every mutating command carries an idempotency key and the client's last seen
// event sequence, which doubles as the optimistic concurrency version.
// Idempotent commands only validate the version on the first execution; a
// retry replays the stored result regardless of the current version.
export class CommandDto {
  @IsString()
  @MaxLength(200)
  requestId: string;

  @Type(() => Number)
  @IsInt()
  @Min(0)
  expectedVersion: number;

  // start / pause / resume / end / changeScene / raiseHand / cancelHand
  @IsString()
  @MaxLength(64)
  command: string;

  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(0)
  @Max(49)
  sceneIndex?: number;

  // Optional display name, used by raiseHand for queue rendering.
  @IsOptional()
  @IsString()
  @MaxLength(200)
  name?: string;

  // Target user for the host-only handleHand command.
  @IsOptional()
  @IsString()
  @MaxLength(200)
  targetUserId?: string;
}
