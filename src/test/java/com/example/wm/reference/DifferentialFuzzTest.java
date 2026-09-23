package com.example.wm.reference;

import com.example.wm.engine.WatermarkCoordinator;
import com.example.wm.model.Classification;
import com.example.wm.model.LateEvent;
import com.example.wm.model.LateReason;
import com.example.wm.model.PartitionStatus;
import com.example.wm.model.PartitionStateView;
import com.example.wm.model.StreamEvent;
import com.example.wm.model.WatermarkConfig;
import com.example.wm.time.VirtualClock;

import org.junit.jupiter.api.RepeatedTest;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

/**
 * 差分测试（fuzzing）：把同一随机操作序列同时喂给
 * 增量引擎 {@link WatermarkCoordinator} 与平铺重放 oracle
 * {@link ReferenceCoordinator}，逐步比对：
 * 全局水位线、各分区状态/有效水位线/迟到计数、事件判定、tick 超时集合。
 *
 * <p>小数据（每轮 <= 60 步、<= 4 分区），正是参考实现的适用范围。
 */
class DifferentialFuzzTest {

    private static final String[] PARTITIONS = {"p0", "p1", "p2", "p3"};

    @RepeatedTest(200)
    void randomScripts_agree() {
        runOneScenario(new Random().nextLong(), false);
    }

    @RepeatedTest(100)
    void randomScriptsWithTicks_agree() {
        runOneScenario(new Random().nextLong(), true);
    }

    /** 固定种子场景：包含大量暂停/恢复与“旧事件重放”。 */
    @Test
    void fixedScenario_pauseResumeHeavy() {
        runOneScenario(0xC0FFEE42L, true);
    }

    /** 极小超时 + 大量跳跃时间，专门压空闲边界与恢复基线。 */
    @Test
    void fixedScenario_tinyTimeout() {
        runOneScenario(7L, true);
    }

    private void runOneScenario(long seed, boolean ticks) {
        Random rng = new Random(seed);
        long bound = rng.nextInt(20);                 // 0..19
        long idleTimeout = 1 + rng.nextInt(120);     // 1..120
        WatermarkConfig config = WatermarkConfig.of(bound, idleTimeout);

        VirtualClock vclock = new VirtualClock(0);
        WatermarkCoordinator engine = new WatermarkCoordinator(config, vclock);
        List<LateEvent> engineLates = new ArrayList<>();
        engine.addLateEventListener(engineLates::add);

        ReferenceCoordinator ref = new ReferenceCoordinator(config);
        Set<String> known = new HashSet<>();
        long simTime = 0;
        List<LateReason> refReasons = new ArrayList<>();

        int steps = 20 + rng.nextInt(41);
        for (int rawStep = 0; rawStep < steps; rawStep++) {
            final int step = rawStep;
            // 时间只前进，偶尔跳跃
            simTime += rng.nextInt(80);
            vclock.advanceTo(simTime);
            ref.setTime(simTime);

            int roll = rng.nextInt(100);
            if (roll < 55) {
                // 事件：多数为近期时间戳（可能乱序），部分从旧范围抽（制造迟到/恢复旧事件）
                String p = PARTITIONS[rng.nextInt(PARTITIONS.length)];
                boolean everSeen = known.contains(p) && ref.seenCount(p) > 0;
                long eventTime;
                if (rng.nextInt(100) < 70 || !everSeen) {
                    long base = Math.max(0, simTime - rng.nextInt(250));
                    eventTime = Math.max(0L, base + (long) (rng.nextInt(3) - 1) * bound);
                } else {
                    // 重放旧范围事件（恢复分区尤其容易命中 RECOVERED_OLD）
                    eventTime = rng.nextInt(Math.max(1, (int) Math.min(Integer.MAX_VALUE, simTime + 1)));
                }
                StreamEvent event = new StreamEvent(p, eventTime, null);

                PartitionStatus statusBefore = ref.statusOf(p);
                boolean wasInactive = statusBefore != null && statusBefore != PartitionStatus.ACTIVE;

                var ev = engine.ingest(event);
                var rv = ref.ingest(event);
                known.add(p); // 收到过事件后才纳入状态比对

                if (rv.classification() == Classification.LATE) {
                    refReasons.add(rv.reason());
                }

                assertEquals(rv.classification(), ev.classification(),
                        () -> msg(seed, step, "classification"));
                assertEquals(rv.reason(), ev.reason(),
                        () -> msg(seed, step, "late reason"));
                assertEquals(rv.resumed(), ev.resumedFromIdle(),
                        () -> msg(seed, step, "resumed"));
                assertEquals(rv.globalWatermarkAfter(), ev.globalWatermark(),
                        () -> msg(seed, step, "post-ingest watermark"));
                assertEquals(new HashSet<>(rv.timedOutAtArrival()),
                        new HashSet<>(ev.timedOutPartitions()),
                        () -> msg(seed, step, "timeout set"));
                // RECOVERED_OLD = 此前收过事件 + 本次属于（含超时即恢复的）重新接入 + 迟到。
                // rv.resumed() 已包含“到达本时间点先超时、随即被事件恢复”的情形，
                // 不能用提前查询的 statusBefore 来判（那时超时检测尚未发生）。
                boolean expectRecoveredOld = everSeen
                        && rv.resumed()
                        && rv.classification() == Classification.LATE;
                assertEquals(expectRecoveredOld,
                        ev.reason() == LateReason.RECOVERED_OLD,
                        () -> msg(seed, step, "RECOVERED_OLD flag"));
            } else if (roll < 70) {
                String p = PARTITIONS[rng.nextInt(PARTITIONS.length)];
                engine.pausePartition(p);
                ref.pause(p);
            } else if (roll < 80) {
                String p = PARTITIONS[rng.nextInt(PARTITIONS.length)];
                Long eff = engine.resumePartition(p);
                Long rgw = ref.resume(p);
                assertEquals(rgw, engine.globalWatermark(), () -> msg(seed, step, "post-resume wm"));
                assertEquals(ref.effectiveWatermark(p), eff,
                        () -> msg(seed, step, "resume effective wm"));
            } else {
                var tv = ref.tick();
                var et = engine.tick();
                assertEquals(new HashSet<>(tv.timedOut()),
                        new HashSet<>(et.timedOutPartitions()),
                        () -> msg(seed, step, "tick timeout"));
                assertEquals(tv.globalWatermarkAfter(), et.globalWatermark(),
                        () -> msg(seed, step, "tick watermark"));
                if (!ticks && roll >= 80) {
                    // 无 tick 分支时不会进入这里；保留以维持分支比例稳定
                }
            }

            assertStateAgree(engine, ref, seed, step, known);
        }

        // 迟到事件总序列一致（两边都按注入顺序记录）
        assertEquals(refReasons.size(), engineLates.size(),
                () -> "late count mismatch seed=" + seed);
        for (int i = 0; i < refReasons.size(); i++) {
            final int idx = i;
            assertEquals(refReasons.get(i), engineLates.get(i).reason(),
                    () -> "late reason #" + idx + " mismatch seed=" + seed);
        }
    }

    private static String msg(long seed, int step, String what) {
        return what + " mismatch seed=" + seed + " step=" + step;
    }

    private void assertStateAgree(WatermarkCoordinator engine, ReferenceCoordinator ref,
                                  long seed, int step0, Set<String> known) {
        final int step = step0;
        assertEquals(ref.globalWatermark(), engine.globalWatermark(),
                () -> "global watermark mismatch seed=" + seed + " step=" + step);

        var snap = engine.snapshot();
        for (String p : known) {
            PartitionStateView view = snap.partitions().stream()
                    .filter(v -> v.partition().equals(p)).findFirst().orElseThrow();
            PartitionStatus rs = ref.statusOf(p);
            assertNotNull(rs);
            assertEquals(rs, view.status(),
                    () -> "status mismatch p=" + p + " seed=" + seed + " step=" + step);
            assertEquals(ref.effectiveWatermark(p), view.effectiveWatermarkMs(),
                    () -> "effective wm mismatch p=" + p + " seed=" + seed + " step=" + step);
            assertEquals(ref.localWatermark(p), view.localWatermarkMs(),
                    () -> "local wm mismatch p=" + p + " seed=" + seed + " step=" + step);
            assertEquals(ref.maxEventTime(p), view.maxEventTimeMs(),
                    () -> "maxEvent mismatch p=" + p + " seed=" + seed + " step=" + step);
            assertEquals(ref.lateCount(p), view.lateEvents(),
                    () -> "late count mismatch p=" + p + " seed=" + seed + " step=" + step);
            assertEquals(ref.seenCount(p), view.seenEvents(),
                    () -> "seen count mismatch p=" + p + " seed=" + seed + " step=" + step);
        }
    }
}
