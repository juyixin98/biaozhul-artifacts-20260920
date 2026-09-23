package dedup.time;

/**
 * 可注入的周期任务调度器。
 *
 * <p>生产环境用 {@link SystemTaskScheduler}（后台守护线程）；
 * 测试用 {@link DeterministicScheduler}：不依赖任何真实线程和墙钟，
 * 由测试在推进手动时钟后显式调用 {@code runDue()} 触发到期任务，
 * 从而可以精确复现“时钟回退”等场景而没有竞态。
 */
public interface TaskScheduler {

    /** 注册一个每 {@code periodMillis} 毫秒执行一次的任务，返回任务句柄。 */
    Cancellable schedulePeriodic(long delayMillis, long periodMillis, Runnable task);

    interface Cancellable {
        void cancel();
    }

    static TaskScheduler system(Clock clock) {
        return new SystemTaskScheduler(clock);
    }

    static DeterministicScheduler deterministic(Clock.ManualClock clock) {
        return new DeterministicScheduler(clock);
    }
}
