package com.example.timeout.clock;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;

class VirtualClockTest {

    private static final Instant START = Instant.parse("2026-09-25T10:00:00Z");

    @Test
    void tickAdvancesWallAndMonotonicTogether() {
        VirtualClock clock = new VirtualClock(START);
        assertEquals(0L, clock.monoNanos());
        assertEquals(START, clock.wall());

        clock.tick(Duration.ofSeconds(30).plusNanos(500));

        assertEquals(Instant.parse("2026-09-25T10:00:30.000000500Z"), clock.wall());
        assertEquals(Duration.ofSeconds(30).plusNanos(500).toNanos(), clock.monoNanos());
    }

    @Test
    void setWallChangesOnlyTheWallClock() {
        VirtualClock clock = new VirtualClock(START);
        clock.tick(Duration.ofMinutes(2));
        long monoBefore = clock.monoNanos();

        // 模拟 NTP 向前校时 3 小时：单调读数必须纹丝不动
        clock.setWall(Instant.parse("2026-09-25T13:00:00Z"));
        assertEquals(monoBefore, clock.monoNanos());
        assertNotEquals(monoBefore, clock.wall().toEpochMilli());

        // 向后校时 2 小时同样不影响单调读数
        clock.advanceWall(Duration.ofHours(-2));
        assertEquals(Instant.parse("2026-09-25T11:00:00Z"), clock.wall());
        assertEquals(monoBefore, clock.monoNanos());
    }
}
