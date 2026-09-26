package com.example.timeout.core;

import com.example.timeout.clock.VirtualClock;
import com.example.timeout.store.JsonTimeoutStore;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class PersistenceRecoveryTest {

    private static final Instant T0 = Instant.parse("2026-09-25T10:00:00Z");

    @TempDir
    Path dir;

    @Test
    void persistedEntriesAreRecomputedFromWallDeadlineOnRestart() throws Exception {
        Path file = dir.resolve("timeouts.json");

        // —— 第一次运行（“旧进程”）——
        VirtualClock clock1 = new VirtualClock(T0);
        var store1 = new JsonTimeoutStore(file);
        var reg1 = TimeoutRegistry.create(clock1);
        reg1.schedule("keep-1h", T0.plusSeconds(3600), "one hour");
        reg1.schedule("keep-90s", T0.plusSeconds(90), "90 seconds");

        // 单调钟推进 30 秒后进程崩溃。持久化内容只含墙钟截止时间，绝不含单调读数。
        clock1.tick(Duration.ofSeconds(30));
        store1.persist(reg1.snapshot());
        String raw = Files.readString(file);
        assertTrue(raw.contains("2026-09-25T11:00:00Z"), "wall deadline must be persisted");
        assertFalse(raw.contains("fireMono"), "monotonic deadline must never be persisted");

        // —— 重启：新进程的单调钟从 0 重新开始 ——
        // 同时墙钟被 NTP 向前校了 2 小时（只影响墙钟基准）。
        VirtualClock clock2 = new VirtualClock(T0.plus(Duration.ofHours(2)));
        var store2 = new JsonTimeoutStore(file);
        var reg2 = TimeoutRegistry.recover(clock2, store2.load());

        // 90 秒的任务：自原始墙钟截止 10:01:30 起，墙钟已到 12:00，重启时必须判定为已过期
        assertEquals(TimeoutStatus.EXPIRED, reg2.status("keep-90s"));
        // 1 小时的任务同理：截止 11:00 已过
        assertEquals(TimeoutStatus.EXPIRED, reg2.status("keep-1h"));
        assertTrue(reg2.pollExpired().stream().anyMatch(e -> e.id().equals("keep-1h")));
    }

    @Test
    void restartBeforeDeadlineRecomputesFreshMonotonicRemaining() throws Exception {
        Path file = dir.resolve("timeouts.json");

        VirtualClock clock1 = new VirtualClock(T0);
        var store1 = new JsonTimeoutStore(file);
        var reg1 = TimeoutRegistry.create(clock1);
        reg1.schedule("future", T0.plusSeconds(600), "ten min");
        store1.persist(reg1.snapshot());

        // 1 分钟后重启（墙钟正常走到 10:01，单调钟归零）
        VirtualClock clock2 = new VirtualClock(T0.plus(Duration.ofMinutes(1)));
        var reg2 = TimeoutRegistry.recover(clock2, new JsonTimeoutStore(file).load());

        assertEquals(TimeoutStatus.SCHEDULED, reg2.status("future"));
        // 关键：剩余 540 秒是用“墙钟截止 - 当前墙钟”重新换算的，
        // 而不是沿用上一个进程单调钟里算出的旧值（旧值还剩 600 秒）
        assertEquals(Duration.ofSeconds(540), reg2.remainingMonotonic("future"));
    }

    @Test
    void missingStoreFileMeansEmptyRegistry() throws Exception {
        var reg = TimeoutRegistry.recover(new VirtualClock(T0),
                new JsonTimeoutStore(dir.resolve("absent.json")).load());
        assertTrue(reg.snapshot().entries().isEmpty());
    }

    @Test
    void expiredRecordStaysExpiredAcrossRestart() throws Exception {
        Path file = dir.resolve("timeouts.json");
        VirtualClock clock1 = new VirtualClock(T0);
        var store1 = new JsonTimeoutStore(file);
        var reg1 = TimeoutRegistry.create(clock1);
        reg1.schedule("past", T0.plusSeconds(10), "10s");
        clock1.tick(Duration.ofSeconds(10));
        reg1.pollExpired(); // 收集过期事件
        store1.persist(reg1.snapshot());

        var reg2 = TimeoutRegistry.recover(new VirtualClock(T0.plus(Duration.ofMinutes(5))),
                new JsonTimeoutStore(file).load());
        assertEquals(TimeoutStatus.EXPIRED, reg2.status("past"));
    }
}
