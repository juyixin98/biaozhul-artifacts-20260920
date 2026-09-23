package com.tjoin.time;

/**
 * 处理时间定时器服务。可注入实现（真实线程调度或手动驱动），
 * 使所有基于处理时间的逻辑（如周期性水位线生成）都可确定性测试。
 */
public interface ProcessingTimeService {

    Clock clock();

    /**
     * 在延迟 {@code delayMillis} 毫秒后执行一次任务。
     *
     * @return 可用于取消的句柄
     */
    ScheduledTask scheduleOnce(long delayMillis, Runnable task);

    /**
     * 周期性执行任务；{@code periodMillis} 必须为正。
     */
    ScheduledTask scheduleAtFixedRate(long initialDelayMillis, long periodMillis, Runnable task);
}
