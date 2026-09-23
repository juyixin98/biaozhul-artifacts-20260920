package com.example.sessionwindow.time;

import java.util.Comparator;
import java.util.PriorityQueue;

/**
 * Deterministic scheduler for tests: time advances only when {@link #advance}
 * is called, and due tasks run synchronously on the calling thread.
 */
public final class ManualTimerService implements TimerService {

    private long now;
    private long nextId;
    private boolean shutdown;

    private static final class Scheduled {
        long id;
        long time;
        long periodMillis; // 0 => one-shot
        Runnable runnable;
        boolean cancelled;

        TimerHandle handle() {
            return new TimerHandle() {
                @Override
                public void cancel() {
                    cancelled = true;
                }

                @Override
                public boolean isCancelled() {
                    return cancelled;
                }
            };
        }
    }

    private final PriorityQueue<Scheduled> queue =
            new PriorityQueue<>(Comparator.comparingLong(s -> s.time));

    public long now() {
        return now;
    }

    @Override
    public TimerHandle scheduleOnce(long delayMillis, Runnable task) {
        return add(now + Math.max(0, delayMillis), 0, task);
    }

    @Override
    public TimerHandle scheduleAtFixedRate(long initialDelayMillis, long periodMillis, Runnable task) {
        return add(now + Math.max(0, initialDelayMillis), Math.max(1, periodMillis), task);
    }

    private TimerHandle add(long time, long period, Runnable task) {
        Scheduled s = new Scheduled();
        s.id = nextId++;
        s.time = time;
        s.periodMillis = period;
        s.runnable = task;
        queue.add(s);
        return s.handle();
    }

    /** Advance virtual time by {@code millis}, running every task due in order. */
    public int advance(long millis) {
        long target = now + millis;
        int ran = 0;
        while (true) {
            Scheduled s = queue.peek();
            if (s == null || s.time > target) {
                break;
            }
            queue.poll();
            now = s.time;
            if (!s.cancelled) {
                s.runnable.run();
                ran++;
                if (s.periodMillis > 0 && !s.cancelled) {
                    s.time += s.periodMillis;
                    queue.add(s);
                }
            }
        }
        now = target;
        return ran;
    }

    public int pendingCount() {
        return (int) queue.stream().filter(s -> !s.cancelled).count();
    }

    @Override
    public void shutdown() {
        shutdown = true;
        queue.clear();
    }

    public boolean isShutdown() {
        return shutdown;
    }
}
