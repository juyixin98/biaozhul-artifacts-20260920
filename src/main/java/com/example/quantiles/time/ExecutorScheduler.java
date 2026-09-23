package com.example.quantiles.time;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * 基于真实线程池的调度器。周期从注册时刻起按墙钟计算，
 * 回调内异常会被吞掉并打印到标准错误，避免周期性任务静默终止。
 */
public final class ExecutorScheduler implements Scheduler {

    private final ScheduledExecutorService executor;
    private final boolean ownsExecutor;

    public ExecutorScheduler() {
        this(Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "quantiles-scheduler");
            t.setDaemon(true);
            return t;
        }), true);
    }

    public ExecutorScheduler(ScheduledExecutorService executor) {
        this(executor, false);
    }

    private ExecutorScheduler(ScheduledExecutorService executor, boolean ownsExecutor) {
        this.executor = executor;
        this.ownsExecutor = ownsExecutor;
    }

    @Override
    public Cancellable schedulePeriodic(long periodMillis, Runnable task) {
        if (periodMillis <= 0) {
            throw new IllegalArgumentException("periodMillis 必须为正数: " + periodMillis);
        }
        var future = executor.scheduleAtFixedRate(() -> {
            try {
                task.run();
            } catch (Throwable t) {
                t.printStackTrace();
            }
        }, periodMillis, periodMillis, TimeUnit.MILLISECONDS);
        return () -> future.cancel(false);
    }

    @Override
    public void shutdown() {
        if (ownsExecutor) {
            executor.shutdownNow();
        }
    }
}
