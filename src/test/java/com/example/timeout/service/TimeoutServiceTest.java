package com.example.timeout.service;

import com.example.timeout.rule.DeadlineRule;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class TimeoutServiceTest {

    private static final Instant T0 = Instant.parse("2026-09-25T10:00:00Z");

    @TempDir
    Path dir;

    private Path storeFile() {
        return dir.resolve("svc-timeouts.json");
    }

    @Test
    void wallAdjustmentsDoNotAffectScheduledTimeoutThroughService() {
        var svc = TimeoutService.virtual(T0, storeFile());
        svc.scheduleFromRule("job", "j", DeadlineRule.duration(Duration.ofMinutes(10)));

        // 向前校时 5 小时，再向后校时 2 小时：超时仍挂着，单调剩余仍为 600 秒
        svc.setWall(T0.plus(Duration.ofHours(5)));
        svc.advanceWall(Duration.ofHours(-2));
        assertEquals(Duration.ofMinutes(10), svc.remainingMonotonic("job"));
        assertTrue(svc.pollExpired().isEmpty());

        // 只有真实流逝（tick）才会让它到期
        svc.tick(Duration.ofMinutes(10));
        assertEquals("EXPIRED", svc.get("job").status().name());
    }

    @Test
    void versionRuleAlreadyExpiredAtScheduleTime() {
        var svc = TimeoutService.virtual(T0, storeFile());
        // 版本 30 分钟前发布，TTL 15 分钟 -> 截止时刻已过 15 分钟
        svc.scheduleFromRule("v1", "stale version",
                DeadlineRule.versionTtl(T0.minus(Duration.ofMinutes(30)), Duration.ofMinutes(15)));

        // 安排返回的只读视图立即呈现 EXPIRED；persist() 作为确认点已确认该事件
        assertEquals("EXPIRED", svc.get("v1").status().name());
        assertTrue(svc.pollExpired().isEmpty(), "already confirmed at the persist boundary");
        assertTrue(svc.list().stream().anyMatch(e -> e.id().equals("v1") && e.isExpired()));
    }

    @Test
    void clockSteeringIsRejectedInSystemMode() {
        var svc = TimeoutService.system(storeFile());
        try {
            svc.tick(Duration.ofSeconds(1));
            throw new AssertionError("system clock must not be steerable");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage().contains("virtual"));
        }
    }
}
