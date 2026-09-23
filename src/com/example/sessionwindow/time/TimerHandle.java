package com.example.sessionwindow.time;

/** Handle to a scheduled task, allowing cancellation. */
public interface TimerHandle {
    void cancel();

    boolean isCancelled();
}
