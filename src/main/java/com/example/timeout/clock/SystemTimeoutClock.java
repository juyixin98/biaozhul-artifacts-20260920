package com.example.timeout.clock;

import java.time.Clock;
import java.time.Instant;

/**
 * 生产环境时钟：墙钟取 UTC 系统时间，单调读数取 {@link System#nanoTime()}。
 * 两者来自不同基准，正体现了真实系统中“墙钟可校、单调钟不可校”的区别。
 */
public final class SystemTimeoutClock implements TimeoutClock {

    private final Clock utc = Clock.systemUTC();

    @Override
    public Instant wall() {
        return Instant.now(utc);
    }

    @Override
    public long monoNanos() {
        return System.nanoTime();
    }

    @Override
    public String type() {
        return "system";
    }
}
