package com.example.wm.engine;

import com.example.wm.model.Classification;
import com.example.wm.model.IngestionResult;
import com.example.wm.model.LateEvent;
import com.example.wm.model.LateReason;
import com.example.wm.model.PartitionStatus;
import com.example.wm.model.StreamEvent;
import com.example.wm.model.WatermarkConfig;
import com.example.wm.time.VirtualClock;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 验收场景：用虚拟时钟验证
 * 1) 分区暂停后全局水位线继续随快分区推进（不被拖住）；
 * 2) 分区恢复后全局水位线不倒退；
 * 3) 恢复分区重放的旧事件明确进入迟到通道（RECOVERED_OLD）；
 * 4) 极端快分区只能把水位线向前推，慢分区决定 min；
 * 5) 全局水位线全程单调不减。
 */
class WatermarkCoordinatorAcceptanceTest {

    private static StreamEvent ev(String p, long t) {
        return new StreamEvent(p, t, "payload@" + t);
    }

    @Test
    void pause_resume_noRegression_recoveredOldEventsGoLate() {
        VirtualClock clock = new VirtualClock(0);
        // B=100, 空闲超时 500（本场景主要用显式 pause/resume）
        var coord = new WatermarkCoordinator(WatermarkConfig.of(100, 500), clock);
        List<Long> globalHistory = new ArrayList<>();
        List<LateEvent> lateSink = new ArrayList<>();
        coord.addLateEventListener(lateSink::add);

        // 两个分区都启动，事件时间 1000 / 1020
        coord.ingest(ev("a", 1000));
        clock.advanceTo(10);
        coord.ingest(ev("b", 1020));
        // 全局 = min(1000-100, 1020-100) = 900
        assertEquals(900L, coord.globalWatermark());
        globalHistory.add(coord.globalWatermark());

        // 暂停 a；b 继续快速推进到 5000
        coord.pausePartition("a");
        clock.advanceTo(20);
        coord.ingest(ev("b", 2000));
        assertEquals(1900L, coord.globalWatermark()); // 只剩 b：2000-100
        globalHistory.add(coord.globalWatermark());

        clock.advanceTo(30);
        coord.ingest(ev("b", 5000));
        assertEquals(4900L, coord.globalWatermark());
        globalHistory.add(coord.globalWatermark());
        assertEquals(PartitionStatus.PAUSED, coord.snapshot().partitions().stream()
                .filter(p -> p.partition().equals("a")).findFirst().orElseThrow().status());

        // 恢复 a，但它先重放一条“旧事件”（事件时间 1500，远低于全局水位线 4900）
        clock.advanceTo(40);
        IngestionResult recovered = coord.ingest(ev("a", 1500));
        assertTrue(recovered.resumedFromIdle(), "暂停分区收到事件应被识别为恢复");
        assertFalse(recovered.accepted(), "恢复重放的旧事件必须进入迟到通道");
        assertEquals(Classification.LATE, recovered.classification());
        // 关键：全局水位线不许倒退
        assertEquals(4900L, coord.globalWatermark(),
                "恢复落后分区不得把全局水位线拉低");
        globalHistory.add(coord.globalWatermark());

        // 迟到事件明确标记为恢复旧事件
        assertEquals(1, lateSink.size());
        assertEquals(LateReason.RECOVERED_OLD, lateSink.get(0).reason());
        assertEquals("a", lateSink.get(0).event().partition());
        assertEquals(1500L, lateSink.get(0).event().eventTimeMs());
        assertEquals(4900L, lateSink.get(0).globalWatermarkMs());

        // a 的本地水位线如实反映数据(1500-100=1400)，但有效水位线被基线抬到 4900
        var viewA = coord.snapshot().partitions().stream()
                .filter(p -> p.partition().equals("a")).findFirst().orElseThrow();
        assertEquals(1400L, viewA.localWatermarkMs());
        assertEquals(4900L, viewA.effectiveWatermarkMs());
        assertEquals(PartitionStatus.ACTIVE, viewA.status());

        // a 追上并超过全局进度：事件时间 6000 -> 本地 5900 > floor 4900，重新参与 min
        clock.advanceTo(50);
        coord.ingest(ev("a", 6000));
        assertEquals(4900L, coord.globalWatermark(), // b 仍停在 4900，min 不变
                "快分区 b 停下后，恢复的 a 追上前 min 仍由 b 决定");
        clock.advanceTo(60);
        coord.ingest(ev("b", 7000)); // b: 6900
        coord.ingest(ev("a", 7100)); // a: 7000
        assertEquals(6900L, coord.globalWatermark());
        globalHistory.add(coord.globalWatermark());

        // 全历史单调不减
        for (int i = 1; i < globalHistory.size(); i++) {
            assertTrue(globalHistory.get(i) >= globalHistory.get(i - 1),
                    "全局水位线倒退: " + globalHistory);
        }
    }

    @Test
    void extremeFastPartition_neverJumpsAheadOfSlowMin_andIsMonotonic() {
        VirtualClock clock = new VirtualClock(0);
        var coord = new WatermarkCoordinator(WatermarkConfig.of(0, Long.MAX_VALUE), clock);

        coord.ingest(ev("slow", 10));
        assertEquals(10L, coord.globalWatermark());

        long previous = 10L;
        // fast 分区时间戳暴涨到百万级
        for (long t = 1_000L; t <= 1_000_000L; t += 1_000L) {
            clock.advanceTo(clock.currentTimeMillis() + 1);
            coord.ingest(ev("fast", t));
            Long w = coord.globalWatermark();
            // slow 停在 10，min 始终是 10：快分区不能越过最慢分区
            assertEquals(10L, w, "慢分区停滞后，全局水位线必须被它钉住");
            assertTrue(w >= previous);
            previous = w;
        }

        // slow 被显式暂停后，fast 才能把全局水位线顶上去
        coord.pausePartition("slow");
        clock.advanceTo(2_000_000);
        coord.ingest(ev("fast", 2_000_000));
        assertEquals(2_000_000L, coord.globalWatermark());
    }

    @Test
    void globalWatermark_neverDecreases_throughoutMixedActivity() {
        VirtualClock clock = new VirtualClock(0);
        var coord = new WatermarkCoordinator(WatermarkConfig.of(50, 10_000), clock);
        List<Long> history = new ArrayList<>();

        long[] eventTimes = {100, 300, 120, 900, 150, 1500, 160, 2000, 5000, 170};
        String[] parts = {"a", "b", "a", "c", "b", "a", "c", "b", "a", "b"};
        for (int i = 0; i < eventTimes.length; i++) {
            clock.advanceTo(i + 1L);
            coord.ingest(ev(parts[i], eventTimes[i]));
            history.add(coord.globalWatermark());
        }
        for (int i = 1; i < history.size(); i++) {
            assertTrue(history.get(i) >= history.get(i - 1),
                    "全局水位线倒退 @%d: %s".formatted(i, history));
        }
    }

    @Test
    void emptyCoordinator_hasNullWatermark_andFirstEventDefinesIt() {
        VirtualClock clock = new VirtualClock(0);
        var coord = new WatermarkCoordinator(WatermarkConfig.of(0, 1000), clock);
        assertNull(coord.globalWatermark());
        coord.ingest(ev("only", 42));
        assertEquals(42L, coord.globalWatermark());
    }
}
