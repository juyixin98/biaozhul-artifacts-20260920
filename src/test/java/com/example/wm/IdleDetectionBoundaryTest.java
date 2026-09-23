package com.example.wm.engine;

import com.example.wm.model.StreamEvent;
import com.example.wm.model.WatermarkConfig;
import com.example.wm.time.VirtualClock;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 空闲判定边界测试（虚拟时钟）：
 * 规则为“含等于”—— now - lastEventTimeMs >= idleTimeoutMs 即 IDLE。
 * 超时前一毫秒仍 ACTIVE；恰好超时即 IDLE；超时期间全局水位线由剩余活跃分区推进。
 */
class IdleDetectionBoundaryTest {

    private static StreamEvent ev(String p, long t) {
        return new StreamEvent(p, t, null);
    }

    @Test
    void idleBoundary_isInclusive_offByOneMillisecond() {
        VirtualClock clock = new VirtualClock(0);
        var coord = new WatermarkCoordinator(WatermarkConfig.of(0, 100), clock);
        coord.ingest(ev("a", 1000)); // 最后活动 @ t=0
        coord.ingest(ev("b", 1000));

        // t=99：差 99 < 100，仍 ACTIVE
        clock.advanceTo(99);
        coord.tick();
        assertEquals("ACTIVE", status(coord, "a"));

        // t=100：差 100 == 100，边界含等于 -> IDLE
        clock.advanceTo(100);
        var tick = coord.tick();
        assertEquals("IDLE", status(coord, "a"));
        assertEquals(List.of("a", "b"), tick.timedOutPartitions(),
                "两个分区同时超时应同时被检出");
    }

    @Test
    void idlePartition_releasesGlobalWatermark() {
        VirtualClock clock = new VirtualClock(0);
        var coord = new WatermarkCoordinator(WatermarkConfig.of(0, 100), clock);
        coord.ingest(ev("slow", 100));  // t=0
        clock.advanceTo(10);
        coord.ingest(ev("fast", 200));  // fast 最后活动 @ t=10
        assertEquals(100L, coord.globalWatermark()); // min(100,200)

        // slow 先在 t=100 超时，全局只剩 fast=200
        clock.advanceTo(100);
        var r1 = coord.tick();
        assertEquals(List.of("slow"), r1.timedOutPartitions());
        assertEquals(200L, coord.globalWatermark());

        // t=109 时 fast 还差 1ms，不超时
        clock.advanceTo(109);
        var r2 = coord.tick();
        assertTrue(r2.timedOutPartitions().isEmpty());
        assertEquals("ACTIVE", status(coord, "fast"));

        // t=110 fast 恰好超时；没有任何活跃分区，全局水位线保持 200（不清空、不倒退）
        clock.advanceTo(110);
        var r3 = coord.tick();
        assertEquals(List.of("fast"), r3.timedOutPartitions());
        assertEquals("IDLE", status(coord, "fast"));
        assertEquals(200L, coord.globalWatermark(),
                "全部空闲时全局水位线保留既有值");
        assertEquals(0, coord.snapshot().activeCount());
    }

    @Test
    void eventArrivalExactlyAtTimeout_resumesAndResetsIdleTimer() {
        VirtualClock clock = new VirtualClock(0);
        var coord = new WatermarkCoordinator(WatermarkConfig.of(0, 100), clock);
        coord.ingest(ev("a", 1000)); // a 最后活动 @ t=0
        clock.advanceTo(10);
        coord.ingest(ev("b", 1000)); // b 最后活动 @ t=10

        // t=110：a 差 110（超时），b 差 100（恰好超时）——两者此刻都超时。
        // 为验证“恰好在超时点到达事件的分区不被误判”，让 b 的事件也在 t=110 注入：
        // 检测按映射迭代顺序先把到点分区标记 IDLE，b 随即被本次事件恢复并刷新计时。
        clock.advanceTo(110);
        var result = coord.ingest(ev("b", 2000));
        assertEquals(2, result.timedOutPartitions().size(),
                "t=110 时 a 与 b 都满足 >=100ms 无事件，检测阶段都被标记");
        assertTrue(result.resumedFromIdle(), "b 检测阶段超时后被本次事件立即恢复");
        assertEquals("ACTIVE", status(coord, "b"));

        // b 的计时从 t=110 重新开始：t=210 差 100 恰好再次超时，t=209 不超时
        clock.advanceTo(209);
        assertTrue(coord.tick().timedOutPartitions().isEmpty());
        clock.advanceTo(210);
        assertEquals(List.of("b"), coord.tick().timedOutPartitions());
    }

    @Test
    void idlePartitionReceivingEvent_isResumedAndGlobalDoesNotRegress() {
        VirtualClock clock = new VirtualClock(0);
        var coord = new WatermarkCoordinator(WatermarkConfig.of(0, 100), clock);
        coord.ingest(ev("a", 100));
        clock.advanceTo(5);
        coord.ingest(ev("b", 100));
        clock.advanceTo(105); // a 超时
        coord.tick();
        // b 推进全局到 5000
        clock.advanceTo(110);
        coord.ingest(ev("b", 5000));
        assertEquals(5000L, coord.globalWatermark());

        // a 以旧事件时间 200 恢复：迟到，全局保持 5000
        var r = coord.ingest(ev("a", 200));
        assertTrue(r.resumedFromIdle());
        assertFalse(r.accepted());
        assertEquals(5000L, coord.globalWatermark());
        assertEquals("ACTIVE", status(coord, "a"));
    }

    private static String status(WatermarkCoordinator c, String name) {
        return c.snapshot().partitions().stream()
                .filter(p -> p.partition().equals(name)).findFirst().orElseThrow()
                .status().name();
    }
}
