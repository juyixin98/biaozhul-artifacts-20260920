import { Injectable } from '@nestjs/common';

function int(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value)) throw new Error(`Invalid integer for ${name}: ${raw}`);
  return value;
}

@Injectable()
export class AppConfig {
  readonly port = int('PORT', 3000);
  readonly databaseUrl =
    process.env.DATABASE_URL ?? 'postgres://stagevault:stagevault@localhost:5432/stagevault';
  readonly sessionIdleTimeoutMs = int('SESSION_IDLE_TIMEOUT_MS', 30 * 60 * 1000);
  readonly timeoutSweepMs = int('TIMEOUT_SWEEP_MS', 10_000);
  readonly maxParticipants = 25;
  readonly maxScenes = 50;
}
