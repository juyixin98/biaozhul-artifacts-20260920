package com.example.wm.time;

/** 系统墙钟，生产环境使用。 */
public final class SystemClock implements Clock {
    @Override
    public long currentTimeMillis() {
        return System.currentTimeMillis();
    }
}
