package streamagg.time;

import java.time.Duration;

/** Injectable scheduler. Production uses {@link SystemScheduler}; deterministic tests use {@link ManualScheduler}. */
public interface Scheduler {

    ScheduledTask schedule(Duration delay, Runnable task);

    ScheduledTask scheduleAtFixedRate(Duration initialDelay, Duration period, Runnable task);
}
