package com.winquant;

/** 基于系统墙钟的时间源。 */
public final class SystemClock implements Clock {

    @Override
    public long now() {
        return System.currentTimeMillis();
    }
}
