package drvb.time;

import java.util.List;

/**
 * 可注入调度器：周期性后台任务（如历史版本自动回收）经由本接口注册。
 *
 * <p>两种实现：
 * <ul>
 *   <li>{@link ManualScheduler}：时间与触发都由测试驱动，确定性执行，无线程；</li>
 *   <li>{@link ExecutorScheduler}：墙钟 + 单线程 {@link java.util.concurrent.ScheduledExecutorService}。</li>
 * </ul>
 *
 * 任务均以"固定周期、首次延迟一个周期"的方式注册，语义参照
 * {@link java.util.concurrent.ScheduledExecutorService#scheduleAtFixedRate}。
 */
public interface Scheduler extends AutoCloseable {

    /**
     * 注册固定周期任务。
     *
     * @param name           任务名（用于查询状态）
     * @param periodMillis   周期（毫秒，正数）
     * @param task           任务体
     * @return 任务句柄
     */
    ScheduledTask scheduleFixedRate(String name, long periodMillis, Runnable task);

    /** 当前已注册任务的快照（用于管理接口）。 */
    List<ScheduledTask> tasks();

    @Override
    void close();

    /** 一个已注册的周期任务句柄。 */
    interface ScheduledTask {
        String name();

        long periodMillis();

        /** 取消任务。 */
        void cancel();

        /** 截至当前时钟的累计触发次数。 */
        long runCount();
    }
}
