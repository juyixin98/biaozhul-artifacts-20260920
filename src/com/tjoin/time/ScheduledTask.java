package com.tjoin.time;

/**
 * 可取消的定时任务句柄。
 */
public interface ScheduledTask {

    /** 取消任务；已执行或已取消则无效。返回取消前是否处于活动（未取消）状态。 */
    boolean cancel();

    boolean isCancelled();
}
