package com.example.timeout.core;

import com.example.timeout.clock.VirtualClock;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class TimeoutRegistryTest {

    private static final Instant T0 = Instant.parse("2026-09-25T10:00:00Z");

    private TimeoutEntry schedule(VirtualClock clock, String id, Instant deadline, String label) {
        return TimeoutRegistry.create(clock).schedule(id, deadline, label);
    }

    @Test
    void convertsWallDeadlineToMonotonicDeadlineExactlyOnce() {
        VirtualClock clock = new VirtualClock(T0);
        var registry = TimeoutRegistry.create(clock);

        TimeoutEntry e = registry.schedule("t1", T0.plus(Duration.ofHours(1)), "one hour");

        assertEquals(Duration.ofHours(1).toNanos(), e.remainingAtConversionNanos());
        assertEquals(Duration.ofHours(1).toNanos(), e.fireMonoNanos());
        // 转换时刻的墙钟读数留痕，便于审计“这是在哪个墙钟瞬间换算的”
        assertEquals(T0, e.convertedAtWall());
        assertEquals(0L, e.convertedAtMonoNanos());
        assertEquals(TimeoutStatus.SCHEDULED, registry.status("t1"));
    }

    @Test
    void forwardWallAdjustmentDoesNotChangeScheduledTimeout() {
        VirtualClock clock = new VirtualClock(T0);
        var registry = TimeoutRegistry.create(clock);
        registry.schedule("t1", T0.plusSeconds(3600), "one hour");

        // NTP 向前跳 3 小时：只改墙钟，单调计时器读数不变
        clock.setWall(T0.plus(Duration.ofHours(3)));

        // 超时仍未触发（若错误地用墙钟差值计时，此刻应已过期）
        assertEquals(TimeoutStatus.SCHEDULED, registry.status("t1"));
        // 距触发的单调剩余时间必须仍是 3600 秒，而不是变成负数或 0
        assertEquals(Duration.ofSeconds(3600), registry.remainingMonotonic("t1"));

        // 单调时间走过 59 分 59 秒：未触发
        clock.tick(Duration.ofSeconds(3599));
        assertEquals(TimeoutStatus.SCHEDULED, registry.status("t1"));
        // 再走 1 秒：在预定的单调时刻触发，与墙钟已经被拨快无关
        clock.tick(Duration.ofSeconds(1));
        assertEquals(TimeoutStatus.EXPIRED, registry.status("t1"));
    }

    @Test
    void backwardWallAdjustmentDoesNotDelayScheduledTimeout() {
        VirtualClock clock = new VirtualClock(T0);
        var registry = TimeoutRegistry.create(clock);
        registry.schedule("t1", T0.plusSeconds(60), "one minute");

        // 管理员把墙钟向后拨 3 小时
        clock.setWall(T0.minus(Duration.ofHours(3)));
        assertEquals(Duration.ofSeconds(60), registry.remainingMonotonic("t1"));

        clock.tick(Duration.ofSeconds(60));
        assertEquals(TimeoutStatus.EXPIRED, registry.status("t1"));
        // 触发瞬间记录的墙钟是 07:01:00Z（被拨慢后的读数），但触发与否只由单调钟决定
        var fired = registry.pollExpired().get(0);
        assertEquals(Instant.parse("2026-09-25T07:01:00Z"), fired.firedAtWall());
    }

    @Test
    void deadlineAlreadyInThePastExpiresImmediately() {
        VirtualClock clock = new VirtualClock(T0);
        var registry = TimeoutRegistry.create(clock);

        TimeoutEntry e = registry.schedule("old", T0.minusSeconds(90), "past deadline");

        assertEquals(TimeoutStatus.EXPIRED, registry.status("old"));
        // 过期 90 秒：剩余为 -90s，用于上报“迟到多久”，而不是被钳成 0 掩盖问题
        assertEquals(Duration.ofSeconds(-90).toNanos(), e.remainingAtConversionNanos());
        assertTrue(registry.pollExpired().stream().anyMatch(x -> x.id().equals("old")));
    }

    @Test
    void tickingDoesNotMixClocksForDuration() {
        // 正常流逝时墙钟与单调钟同步前进；剩余时间只从单调钟推算
        VirtualClock clock = new VirtualClock(T0);
        var registry = TimeoutRegistry.create(clock);
        registry.schedule("t1", T0.plusSeconds(120), "x");

        clock.tick(Duration.ofSeconds(30));
        assertEquals(Duration.ofSeconds(90), registry.remainingMonotonic("t1"));

        // 再做一次向后校时，剩余单调时间不因墙钟读数而变
        clock.setWall(clock.wall().minus(Duration.ofMinutes(15)));
        assertEquals(Duration.ofSeconds(90), registry.remainingMonotonic("t1"));
    }

    @Test
    void rejectsDuplicateIdAndUnknownIdLookups() {
        VirtualClock clock = new VirtualClock(T0);
        var registry = TimeoutRegistry.create(clock);
        registry.schedule("dup", T0.plusSeconds(10), "first");
        assertTrue(assertThrowsIllegalArg(() ->
                registry.schedule("dup", T0.plusSeconds(20), "second")).contains("dup"));
        assertTrue(assertThrowsIllegalArg(() -> registry.status("nope")).contains("nope"));
    }

    private static String assertThrowsIllegalArg(Runnable r) {
        try {
            r.run();
            throw new AssertionError("expected IllegalArgumentException");
        } catch (IllegalArgumentException e) {
            return e.getMessage();
        }
    }
}
