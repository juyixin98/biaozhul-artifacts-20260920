package com.example.quantiles.time;

/**
 * 可注入的调度器：负责周期性触发回调（例如周期性生成 watermark）。
 *
 * <p>生产环境使用 {@link ExecutorScheduler}（基于 {@link java.util.concurrent.ScheduledExecutorService}），
 * 测试中使用 {@link ManualScheduler}（时间与触发均由测试手动推进，确定性执行）。
 */
public interface Scheduler {

    /** 以固定周期（毫秒）调度任务，返回可用于取消的句柄。 */
    Cancellable schedulePeriodic(long periodMillis, Runnable task);

    /** 释放调度器占用的线程资源。 */
    void shutdown();

    /** 取消句柄。 */
    @FunctionalInterface
    interface Cancellable {
        void cancel();
    }
}
