package com.example.watermark.time;

import java.util.PriorityQueue;

/**
 * Deterministic scheduler driven by a {@link VirtualClock}. Timers live in a
 * priority queue ordered by fire time; they only execute when the clock ticks
 * to (or past) their fire time. No real threads, no real waiting.
 *
 * <p>Either register this scheduler as a tick listener on the clock
 * ({@code clock.addTickListener(scheduler::runDue)}) or call
 * {@link #runDue()} manually.
 */
public final class ManualScheduler implements Scheduler {

    private static final class Timer implements ScheduledTask {
        long nextFireTime;
        final long period; // Long.MIN_VALUE marks a one-shot timer
        final Runnable task;
        boolean cancelled;

        Timer(long nextFireTime, long period, Runnable task) {
            this.nextFireTime = nextFireTime;
            this.period = period;
            this.task = task;
        }

        @Override
        public void cancel() {
            cancelled = true;
        }

        @Override
        public boolean isCancelled() {
            return cancelled;
        }
    }

    private final Clock clock;
    private final PriorityQueue<Timer> timers =
            new PriorityQueue<>(java.util.Comparator.comparingLong(t -> t.nextFireTime));

    public ManualScheduler(Clock clock) {
        this.clock = clock;
    }

    @Override
    public ScheduledTask schedulePeriodically(Runnable task, long initialDelayMillis, long periodMillis) {
        if (initialDelayMillis < 0) {
            throw new IllegalArgumentException("initialDelay must be non-negative: " + initialDelayMillis);
        }
        if (periodMillis <= 0) {
            throw new IllegalArgumentException("period must be positive: " + periodMillis);
        }
        Timer timer = new Timer(clock.currentTimeMillis() + initialDelayMillis, periodMillis, task);
        timers.add(timer);
        return timer;
    }

    /**
     * Run all due, non-cancelled timers, in fire-time order. A periodic timer
     * fires once for every scheduled fire time reached within a single clock
     * jump (catch-up), each at its scheduled time relative to task logic that
     * reads the clock — note that tasks observing {@link Clock#currentTimeMillis()}
     * see the post-jump time on all catch-up firings.
     */
    public void runDue() {
        long now = clock.currentTimeMillis();
        while (!timers.isEmpty() && timers.peek().nextFireTime <= now) {
            Timer timer = timers.poll();
            if (timer.cancelled) {
                continue;
            }
            timer.task.run();
            // Re-queue immediately so all due periods of a jump fire in order;
            // re-check cancelled() in case the task cancelled itself.
            if (!timer.cancelled) {
                timer.nextFireTime += timer.period;
                timers.add(timer);
            }
        }
    }

    /** Clock time of the next pending timer, or {@link Long#MAX_VALUE} if none. */
    public long nextFireTime() {
        while (!timers.isEmpty() && timers.peek().cancelled) {
            timers.poll();
        }
        return timers.isEmpty() ? Long.MAX_VALUE : timers.peek().nextFireTime;
    }
}
