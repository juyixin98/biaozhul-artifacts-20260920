package com.example.wm.time;

/** 已调度任务的句柄。 */
public interface ScheduledTask {
    void cancel();

    boolean isCancelled();
}
