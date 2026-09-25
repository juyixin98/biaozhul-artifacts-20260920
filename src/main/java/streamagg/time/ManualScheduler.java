package streamagg.time;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.PriorityQueue;

/**
 * Deterministic scheduler driven by a {@link ManualClock}. Nothing runs until a test
 * calls {@link #advance(Duration)}; due tasks fire at their scheduled instant, in
 * chronological order. Periodic tasks may fire multiple times within one advance.
 */
public final class ManualScheduler implements Scheduler {

    private final ManualClock clock;
    private final PriorityQueue<Task> tasks = new PriorityQueue<>(Comparator.comparing(t -> t.fireAt));
    private long idGen;

    public ManualScheduler(ManualClock clock) {
        this.clock = clock;
    }

    @Override
    public ScheduledTask schedule(Duration delay, Runnable task) {
        Task t = new Task(clock.instant().plus(delay), null, task);
        tasks.add(t);
        return t;
    }

    @Override
    public ScheduledTask scheduleAtFixedRate(Duration initialDelay, Duration period, Runnable task) {
        Task t = new Task(clock.instant().plus(initialDelay), period, task);
        tasks.add(t);
        return t;
    }

    /** Moves the clock to now + {@code delta}, running every task due in between. Returns number of task runs. */
    public int advance(Duration delta) {
        Instant target = clock.instant().plus(delta);
        int fired = 0;
        while (true) {
            tasks.removeIf(t -> t.cancelled);
            Task t = tasks.peek();
            if (t == null || t.fireAt.isAfter(target)) {
                break;
            }
            tasks.poll();
            clock.setTime(t.fireAt);
            t.task.run();
            fired++;
            if (t.period != null && !t.cancelled) {
                t.fireAt = t.fireAt.plus(t.period);
                tasks.add(t);
            }
        }
        clock.setTime(target);
        return fired;
    }

    /** Scheduled instants still pending (diagnostics for tests). */
    public List<Instant> pendingInstants() {
        List<Instant> out = new ArrayList<>();
        for (Task t : tasks) {
            if (!t.cancelled) {
                out.add(t.fireAt);
            }
        }
        return out;
    }

    private final class Task implements ScheduledTask {
        volatile Instant fireAt;
        final Duration period;
        final Runnable task;
        final long id;
        volatile boolean cancelled;

        Task(Instant fireAt, Duration period, Runnable task) {
            this.fireAt = fireAt;
            this.period = period;
            this.task = task;
            this.id = ++idGen;
        }

        @Override
        public void cancel() {
            cancelled = true;
        }
    }
}
