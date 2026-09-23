package com.example.quantiles.time;

/** 基于 {@link System#currentTimeMillis()} 的墙钟实现。 */
public final class SystemClock implements Clock {

    @Override
    public long nowMillis() {
        return System.currentTimeMillis();
    }
}
