package com.example.sessionwindow.time;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Real scheduler backed by a daemon single-thread executor per instance.
 */
public final class ScheduledTimerService implements TimerService {

    private final ScheduledExecutorService executor;

    public ScheduledTimerService() {
        this(Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "session-window-timer");
            t.setDaemon(true);
            return t;
        }));
    }

    public ScheduledTimerService(ScheduledExecutorService executor) {
        this.executor = executor;
    }

    @Override
    public TimerHandle scheduleOnce(long delayMillis, Runnable task) {
        ScheduledFuture<?> future =
                executor.schedule(task, Math.max(0, delayMillis), TimeUnit.MILLISECONDS);
        return new Handle(future);
    }

    @Override
    public TimerHandle scheduleAtFixedRate(long initialDelayMillis, long periodMillis, Runnable task) {
        ScheduledFuture<?> future = executor.scheduleAtFixedRate(
                task,
                Math.max(0, initialDelayMillis),
                Math.max(1, periodMillis),
                TimeUnit.MILLISECONDS);
        return new Handle(future);
    }

    @Override
    public void shutdown() {
        executor.shutdownNow();
    }

    private record Handle(ScheduledFuture<?> future) implements TimerHandle {
        @Override
        public void cancel() {
            future.cancel(false);
        }

        @Override
        public boolean isCancelled() {
            return future.isCancelled();
        }
    }
}
