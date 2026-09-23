package approx.time;

import java.util.ArrayList;
import java.util.List;

/**
 * Deterministic clock + scheduler for tests.
 *
 * <p>Nothing runs on a background thread: registered periodic tasks fire when
 * {@link #advance(long)} moves virtual time across (or onto) their next due
 * instant. Multiple tasks due at the same instant run in registration order.
 */
public final class ManualEnvironment implements Clock, TaskScheduler {

    private static final class Task {
        final long period;
        final Runnable runnable;
        long nextDue;
        boolean cancelled;

        Task(long period, Runnable runnable, long firstDue) {
            this.period = period;
            this.runnable = runnable;
            this.nextDue = firstDue;
        }
    }

    private long now;
    private final List<Task> tasks = new ArrayList<>();

    public ManualEnvironment(long startMillis) {
        this.now = startMillis;
    }

    @Override
    public synchronized long nowMillis() {
        return now;
    }

    @Override
    public synchronized CancellationToken schedulePeriodic(long periodMillis, Runnable task) {
        return schedulePeriodic(periodMillis, periodMillis, task);
    }

    @Override
    public synchronized CancellationToken schedulePeriodic(
            long initialDelayMillis, long periodMillis, Runnable task) {
        long period = Math.max(1L, periodMillis);
        long first = Math.max(1L, initialDelayMillis);
        Task t = new Task(period, task, now + first);
        tasks.add(t);
        return () -> {
            synchronized (ManualEnvironment.this) {
                t.cancelled = true;
            }
        };
    }

    /** Move virtual time forward by {@code deltaMillis}, firing any due tasks. */
    public synchronized void advance(long deltaMillis) {
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("time cannot move backwards");
        }
        long target = now + deltaMillis;
        // Fire in chronological order; tasks sharing an instant fire in registration order.
        while (true) {
            long next = Long.MAX_VALUE;
            Task due = null;
            for (Task t : tasks) {
                if (!t.cancelled && t.nextDue <= target && t.nextDue < next) {
                    next = t.nextDue;
                    due = t;
                }
            }
            if (due == null) {
                break;
            }
            now = due.nextDue;
            due.nextDue += due.period;
            // Reentrant callbacks (e.g. a tick reading the clock) run on the same thread.
            due.runnable.run();
        }
        now = target;
    }

    @Override
    public void close() {
        tasks.clear();
    }
}
