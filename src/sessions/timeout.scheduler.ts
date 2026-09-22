import {
  Injectable,
  Logger,
  OnModuleDestroy,
  OnModuleInit,
} from '@nestjs/common';
import { AppConfig } from '../config/app-config';
import { SessionsService } from './sessions.service';

// Periodic inactivity sweeper. Deadlines are persisted on the session row, so
// this is stateless work: on restart the first sweep after boot resumes all
// pending timeouts without any in-memory timer bookkeeping.
@Injectable()
export class TimeoutScheduler implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(TimeoutScheduler.name);
  private timer: NodeJS.Timeout | null = null;
  private sweeping: Promise<void> | null = null;

  constructor(
    private readonly sessions: SessionsService,
    private readonly config: AppConfig,
  ) {}

  onModuleInit(): void {
    this.timer = setInterval(() => {
      // Never overlap sweeps.
      if (this.sweeping) return;
      this.sweeping = this.run().finally(() => {
        this.sweeping = null;
      });
    }, this.config.timeoutSweepMs);
    this.timer.unref?.();
    // Run once shortly after boot so restarted timeout tasks are handled even
    // if the sweep interval itself is long.
    setTimeout(() => {
      this.sweeping = this.run().finally(() => {
        this.sweeping = null;
      });
    }, Math.min(500, this.config.timeoutSweepMs)).unref?.();
  }

  onModuleDestroy(): void {
    if (this.timer) clearInterval(this.timer);
  }

  private async run(): Promise<void> {
    try {
      const ended = await this.sessions.sweepTimeouts();
      if (ended > 0) this.logger.log(`Auto-ended ${ended} inactive session(s)`);
    } catch (error) {
      this.logger.error(`Timeout sweep error: ${String(error)}`);
    }
  }
}
