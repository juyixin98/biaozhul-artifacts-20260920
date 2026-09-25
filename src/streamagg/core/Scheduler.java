package streamagg.core;

/**
 * 可注入的调度器。
 * 生产环境使用 {@link RealScheduler}（ScheduledExecutorService 驱动）；
 * 测试使用 {@link ManualScheduler}（测试手动触发到期任务，不依赖真实时间与线程）。
 */
public interface Scheduler {
    /**
     * 以固定周期（毫秒）执行任务，第一次执行发生在 {@code initialDelayMillis} 之后。
     */
    Cancellable scheduleAtFixedRate(Runnable task, long initialDelayMillis, long periodMillis);
}
