package com.example.sessionwindow.service;

/**
 * Configuration of one running pipeline.
 *
 * @param gap              session inactivity gap (event-time milliseconds)
 * @param allowedLateness  how long after a window's end it stays reopenable
 * @param autoWatermark    if true, watermarks are derived from observed event
 *                         timestamps (punctuated); if false, callers must send
 *                         watermark items explicitly
 * @param outOfOrderness   punctuated watermark lag behind max timestamp
 */
public record ServiceConfig(long gap, long allowedLateness, boolean autoWatermark,
                            long outOfOrderness) {

    public static ServiceConfig defaults() {
        return new ServiceConfig(10L, 0L, true, 0L);
    }
}
