package com.example.wm.time;

/**
 * 可注入的定时器调度器：引擎只依赖该接口注册周期任务，
 * 测试使用 {@link ManualScheduler} 由测试线程手动触发，
 * 生产使用 {@link SystemScheduler} 基于真实的 ScheduledExecutorService。
 */
public interface Scheduler extends AutoCloseable {
    /**
     * 注册一个固定延迟的周期任务。
     *
     * @param task     要执行的任务
     * @param delayMs  首次执行延迟
     * @param periodMs 后续周期间隔（必须为正）
     * @return 可取消该任务的句柄
     */
    ScheduledTask schedulePeriodic(Runnable task, long delayMs, long periodMs);

    @Override
    void close();
}
