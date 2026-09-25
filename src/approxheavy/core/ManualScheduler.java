package approxheavy.core;

import java.util.ArrayList;
import java.util.List;

/** Scheduler whose tasks are only run when tests ask. Time stays deterministic. */
public final class ManualScheduler implements Scheduler {
    private static final class Task {
        final long id;
        final long periodMillis;
        final Runnable runnable;

        Task(long id, long periodMillis, Runnable runnable) {
            this.id = id;
            this.periodMillis = periodMillis;
            this.runnable = runnable;
        }
    }

    private final List<Task> tasks = new ArrayList<>();
    private long nextId = 1;

    @Override
    public synchronized long scheduleAtFixedRate(long periodMillis, Runnable task) {
        long id = nextId++;
        tasks.add(new Task(id, periodMillis, task));
        return id;
    }

    @Override
    public synchronized void cancel(long id) {
        tasks.removeIf(t -> t.id == id);
    }

    /** Run every registered periodic task exactly once (used by tests). */
    public synchronized void runAllOnce() {
        for (Task t : new ArrayList<>(tasks)) {
            t.runnable.run();
        }
    }

    public synchronized int taskCount() {
        return tasks.size();
    }
}
