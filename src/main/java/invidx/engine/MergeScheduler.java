package invidx.engine;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Single-threaded timer driving background merge attempts. A separate
 * {@code running} gate guarantees that a manual merge and a scheduled tick
 * never build concurrently.
 */
final class MergeScheduler {

    private final IndexConfig config;
    private final Runnable tick;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private ScheduledExecutorService executor;
    MergeScheduler(IndexConfig config, Runnable tick) {
        this.config = config;
        this.tick = tick;
    }

    boolean claim() {
        return running.compareAndSet(false, true);
    }

    void release() {
        running.set(false);
    }

    boolean isRunning() {
        return running.get();
    }

    synchronized void start() {
        if (!config.backgroundMerge() || executor != null) {
            return;
        }
        executor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "index-merge");
            t.setDaemon(true);
            return t;
        });
        long ms = Math.max(5, config.mergeInterval().toMillis());
        executor.scheduleWithFixedDelay(this::safeTick, ms, ms, TimeUnit.MILLISECONDS);
    }

    private void safeTick() {
        try {
            tick.run();
        } catch (Throwable t) {
            // Scheduled tasks die silently after an exception; keep the timer alive.
            System.getLogger(MergeScheduler.class.getName())
                    .log(System.Logger.Level.WARNING, "merge tick failed", t);
        }
    }

    synchronized void stop() {
        if (executor != null) {
            executor.shutdownNow();
            try {
                executor.awaitTermination(5, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            executor = null;
        }
        running.set(false);
    }
}
