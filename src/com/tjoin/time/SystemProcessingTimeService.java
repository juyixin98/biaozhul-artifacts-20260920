package com.tjoin.time;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * 基于单线程 {@link ScheduledExecutorService} 的真实处理时间服务。
 */
public final class SystemProcessingTimeService implements ProcessingTimeService, AutoCloseable {

    private final Clock clock;
    private final ScheduledExecutorService executor;
    private final boolean ownsExecutor;

    public SystemProcessingTimeService() {
        this(Clock.system(), Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "tjoin-processing-time");
            t.setDaemon(true);
            return t;
        }), true);
    }

    public SystemProcessingTimeService(Clock clock, ScheduledExecutorService executor, boolean ownsExecutor) {
        this.clock = clock;
        this.executor = executor;
        this.ownsExecutor = ownsExecutor;
    }

    @Override
    public Clock clock() {
        return clock;
    }

    @Override
    public ScheduledTask scheduleOnce(long delayMillis, Runnable task) {
        ScheduledFuture<?> f = executor.schedule(task, Math.max(0, delayMillis), TimeUnit.MILLISECONDS);
        return new FutureTask(f);
    }

    @Override
    public ScheduledTask scheduleAtFixedRate(long initialDelayMillis, long periodMillis, Runnable task) {
        if (periodMillis <= 0) {
            throw new IllegalArgumentException("period must be positive");
        }
        ScheduledFuture<?> f = executor.scheduleAtFixedRate(
                task, Math.max(0, initialDelayMillis), periodMillis, TimeUnit.MILLISECONDS);
        return new FutureTask(f);
    }

    @Override
    public void close() {
        if (ownsExecutor) {
            executor.shutdownNow();
        }
    }

    private static final class FutureTask implements ScheduledTask {
        private final ScheduledFuture<?> future;

        FutureTask(ScheduledFuture<?> future) {
            this.future = future;
        }

        @Override
        public boolean cancel() {
            return future.cancel(false);
        }

        @Override
        public boolean isCancelled() {
            return future.isCancelled();
        }
    }
}
