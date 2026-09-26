package com.example.monotime;

import java.time.Instant;

/** 生产环境时钟：墙钟取 {@link Instant#now()}，单调读数取 {@link System#nanoTime()}。 */
public final class SystemClockPort implements ClockPort {

    @Override
    public long monotonicNanos() {
        return System.nanoTime();
    }

    @Override
    public Instant wallClockInstant() {
        return Instant.now();
    }
}
