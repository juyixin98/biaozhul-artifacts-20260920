package com.tjoin.core;

import static com.tjoin.Asserts.assertEquals;
import static com.tjoin.Asserts.assertFalse;
import static com.tjoin.Asserts.assertNotNull;
import static com.tjoin.Asserts.assertThrows;
import static com.tjoin.Asserts.assertTrue;

import com.tjoin.Test;

import java.util.ArrayList;
import java.util.List;

/**
 * {@link IntervalJoinOperator} 核心语义测试，重点覆盖：
 * 区间匹配、时间边界、两侧独立水位线、一侧停滞、迟到、去重、缓冲上限。
 */
public class IntervalJoinOperatorTest {

    private static Event e(String id, StreamSide side, String key, long ts) {
        return new Event(id, side, key, ts, id + "-val");
    }

    private static IntervalJoinOperator op(long lower, long upper) {
        return new IntervalJoinOperator(JoinConfig.builder().lowerBound(lower).upperBound(upper).build());
    }

    private List<JoinPair> feed(IntervalJoinOperator op, Event... events) {
        List<JoinPair> out = new ArrayList<>();
        for (Event ev : events) {
            op.processEvent(ev, out::add);
        }
        return out;
    }

    private JoinPair onlyPair(List<JoinPair> out, String leftId, String rightId) {
        JoinPair found = null;
        for (JoinPair p : out) {
            if (p.left().id().equals(leftId) && p.right().id().equals(rightId)) {
                assertTrue(found == null, "duplicate pair " + p);
                found = p;
            }
        }
        assertNotNull(found, "missing pair " + leftId + "/" + rightId);
        return found;
    }

    @Test
    public void matchesInBothArrivalOrders() {
        // 左先到
        IntervalJoinOperator o1 = op(-2L, 2L);
        List<JoinPair> a = feed(o1,
                e("L1", StreamSide.LEFT, "k", 10),
                e("R1", StreamSide.RIGHT, "k", 11));
        assertEquals(1, a.size(), "right arrives later -> one pair");

        // 右先到
        IntervalJoinOperator o2 = op(-2L, 2L);
        List<JoinPair> b = feed(o2,
                e("R1", StreamSide.RIGHT, "k", 11),
                e("L1", StreamSide.LEFT, "k", 10));
        assertEquals(1, b.size(), "left arrives later -> one pair");

        // 超出区间不匹配
        IntervalJoinOperator o3 = op(-2L, 2L);
        List<JoinPair> c = feed(o3,
                e("L1", StreamSide.LEFT, "k", 10),
                e("R2", StreamSide.RIGHT, "k", 13));
        assertTrue(c.isEmpty(), "diff=3 > upper=2 -> no match");
    }

    @Test
    public void boundariesInclusiveByDefault() {
        IntervalJoinOperator o = op(-2L, 2L);
        List<JoinPair> out = feed(o,
                e("L", StreamSide.LEFT, "k", 10),
                e("Rlo", StreamSide.RIGHT, "k", 8),   // -2
                e("Rmid", StreamSide.RIGHT, "k", 10), // 0
                e("Rhi", StreamSide.RIGHT, "k", 12)); // +2
        assertEquals(3, out.size(), "both boundaries included");
    }

    @Test
    public void exclusiveBoundariesExcludeExactEdges() {
        JoinConfig cfg = JoinConfig.builder()
                .lowerBound(-2L).upperBound(2L)
                .lowerInclusive(false).upperInclusive(false)
                .build();
        IntervalJoinOperator o = new IntervalJoinOperator(cfg);
        List<JoinPair> out = feed(o,
                e("L", StreamSide.LEFT, "k", 10),
                e("Rlo", StreamSide.RIGHT, "k", 8),
                e("Rmid", StreamSide.RIGHT, "k", 10),
                e("Rhi", StreamSide.RIGHT, "k", 12));
        assertEquals(1, out.size(), "exclusive: only diff 0 matches");
        assertEquals("Rmid", out.get(0).right().id(), "inner event matches");
    }

    @Test
    public void keysAreIsolated() {
        IntervalJoinOperator o = op(-100L, 100L);
        List<JoinPair> out = feed(o,
                e("L1", StreamSide.LEFT, "a", 10),
                e("R1", StreamSide.RIGHT, "b", 10));
        assertTrue(out.isEmpty(), "different keys never join");
        List<JoinPair> out2 = feed(o, e("R2", StreamSide.RIGHT, "a", 10));
        assertEquals(1, out2.size(), "same key joins across buffers");
    }

    // ----------------------------------------------------------------
    // 水位线：独立推进 + 一侧停滞
    // ----------------------------------------------------------------

    @Test
    public void stalledRightWatermarkKeepsLeftRecords() {
        IntervalJoinOperator o = op(0L, 5L);
        feed(o,
                e("L1", StreamSide.LEFT, "k", 10),
                e("L2", StreamSide.LEFT, "k", 12));
        o.processWatermark(StreamSide.LEFT, 1000); // 左流跑得很远
        assertEquals(2, o.bufferedCount(StreamSide.LEFT),
                "left wm alone must NOT expire left records (need right wm)");
        assertEquals(Long.MIN_VALUE, o.watermark(StreamSide.RIGHT), "right wm still initial");
        assertEquals(Long.MIN_VALUE, o.outputWatermark(), "output wm = min(L,R) = MIN_VALUE");

        // 右流恢复，仍能匹配边界事件
        List<JoinPair> late = feed(o, e("R1", StreamSide.RIGHT, "k", 15));
        assertEquals(2, late.size(), "R1@15 matches L1@10 (upper edge) and L2@12");
    }

    @Test
    public void stalledLeftWatermarkKeepsRightRecords() {
        IntervalJoinOperator o = op(0L, 5L);
        feed(o, e("R1", StreamSide.RIGHT, "k", 10));
        o.processWatermark(StreamSide.RIGHT, 1000); // 只有右水位线推进
        assertEquals(1, o.bufferedCount(StreamSide.RIGHT),
                "right wm alone must NOT expire right records (need left wm)");

        // 左水位线到达 5：R1@10 对未来左事件 l 要求 l.ts <= 10 且 l.ts >= 5；
        // 未来左事件 ts >= 5，下界条件 10 - wmL < lower(0) 尚未满足 → 仍保留
        o.processWatermark(StreamSide.LEFT, 5);
        assertEquals(1, o.bufferedCount(StreamSide.RIGHT), "R1 still matchable by L@5 (lower edge)");
        List<JoinPair> hit = feed(o, e("L5", StreamSide.LEFT, "k", 5));
        assertEquals(1, hit.size(), "L@5 matches R@10 on lower edge");

        // 左水位线 6：ts>=6 的左事件，R@10 差 4，仍在 [0,5]；左水位线 11 时差 -1 < 0 → 过期
        o.processWatermark(StreamSide.LEFT, 11);
        assertEquals(0, o.bufferedCount(StreamSide.RIGHT), "R1 expired once left wm passes 10");
        assertEquals(1L, o.metrics().rightExpired(), "expired counter");
    }

    @Test
    public void rightWatermarkExpiresLeftOnlyAtCorrectPoint() {
        IntervalJoinOperator o = op(0L, 5L);
        feed(o, e("L1", StreamSide.LEFT, "k", 10));

        o.processWatermark(StreamSide.RIGHT, 15); // wmR - L1 = 5，闭边界，尚未过期
        assertEquals(1, o.bufferedCount(StreamSide.LEFT), "wmR=15 keeps L1 (upper edge inclusive)");

        o.processWatermark(StreamSide.RIGHT, 16); // 差 6 > 5 → 过期
        assertEquals(0, o.bufferedCount(StreamSide.LEFT), "wmR=16 expires L1");

        // 过期后迟到的、本可匹配的事件不再产生输出
        List<JoinPair> out = feed(o, e("R1", StreamSide.RIGHT, "k", 10));
        assertTrue(out.isEmpty(), "no zombie match after L1 expired");
    }

    @Test
    public void expiredRecordRedeliveryDoesNotRejoin() {
        IntervalJoinOperator o = op(0L, 5L);
        feed(o, e("L1", StreamSide.LEFT, "k", 10));
        o.processWatermark(StreamSide.RIGHT, 16); // L1 过期
        assertEquals(0, o.bufferedCount(StreamSide.LEFT), "L1 expired");

        // 重放同一 ID（过期事件重放）：去重记忆仍在（ts=10 < rightWm? 不，seen 按本侧 wm 淘汰）
        List<JoinPair> replay = feed(o, e("L1", StreamSide.LEFT, "k", 10));
        assertTrue(replay.isEmpty(), "redelivered expired event ignored (no rebuffer/duplicate output)");
        assertEquals(1L, o.metrics().duplicates(), "replay counted as duplicate");

        // 再推进左水位线超过 10，seen 记忆才被淘汰；但之后到达的“新事件”语义不影响本断言
        o.processWatermark(StreamSide.LEFT, 11);
        List<JoinPair> replayAfterPrune = feed(o, e("L1", StreamSide.LEFT, "k", 10));
        assertTrue(replayAfterPrune.isEmpty(), "after prune it is simply late, still no output");
        assertEquals(1L, o.metrics().lateDropped(), "late drop counter");
    }

    @Test
    public void watermarksDoNotRegress() {
        IntervalJoinOperator o = op(0L, 5L);
        o.processWatermark(StreamSide.LEFT, 100);
        o.processWatermark(StreamSide.LEFT, 50);
        assertEquals(100, o.watermark(StreamSide.LEFT), "stale wm ignored");
        assertEquals(1L, o.metrics().staleWatermarks(), "stale counter");
    }

    @Test
    public void lateEventsAreDroppedAndCounted() {
        IntervalJoinOperator o = op(0L, 100L);
        o.processWatermark(StreamSide.LEFT, 20);
        List<JoinPair> out = new ArrayList<>();
        o.processEvent(e("Llate", StreamSide.LEFT, "k", 19), out::add);
        assertTrue(out.isEmpty(), "ts < wm => late, no output");
        assertEquals(1L, o.metrics().lateDropped(), "late counter");

        // 恰等于水位线不算迟到
        List<JoinPair> edge = new ArrayList<>();
        o.processEvent(e("Ledge", StreamSide.LEFT, "k", 20), edge::add);
        assertEquals(0L, o.metrics().lateDropped() - 1, "ts == wm is on time");
        assertEquals(1, o.bufferedCount(StreamSide.LEFT), "on-time edge event buffered");
    }

    // ----------------------------------------------------------------
    // 去重 / 每对仅一次
    // ----------------------------------------------------------------

    @Test
    public void duplicateValuesJoinButDuplicateIdsDoNot() {
        IntervalJoinOperator o = op(-10L, 10L);
        List<JoinPair> out = new ArrayList<>();
        o.processEvent(e("L1", StreamSide.LEFT, "k", 10), out::add);
        o.processEvent(e("L2", StreamSide.LEFT, "k", 10), out::add); // 同值同时间，不同 ID
        o.processEvent(e("R1", StreamSide.RIGHT, "k", 10), out::add);
        assertEquals(2, out.size(), "two distinct left events each join R1");

        int before = out.size();
        o.processEvent(e("R1", StreamSide.RIGHT, "k", 10), out::add); // 重放
        o.processEvent(e("L1", StreamSide.LEFT, "k", 10), out::add); // 重放
        assertEquals(before, out.size(), "redelivered events create no pairs");
        assertEquals(2L, o.metrics().duplicates(), "two duplicates counted");
        assertEquals(out.size(), out.stream().distinct().count(), "all pairs distinct");
    }

    @Test
    public void pairEmittedAtMostOnceAcrossManyRedeliveries() {
        IntervalJoinOperator o = op(-100L, 100L);
        List<JoinPair> out = new ArrayList<>();
        o.processEvent(e("L1", StreamSide.LEFT, "k", 1), out::add);
        o.processEvent(e("R1", StreamSide.RIGHT, "k", 1), out::add);
        for (int i = 0; i < 10; i++) {
            o.processEvent(e("L1", StreamSide.LEFT, "k", 1), out::add);
            o.processEvent(e("R1", StreamSide.RIGHT, "k", 1), out::add);
        }
        assertEquals(1, out.size(), "the one pair emitted exactly once");
    }

    @Test
    public void allPairsEmittedForCartesianWithinInterval() {
        IntervalJoinOperator o = op(0L, 2L);
        List<JoinPair> out = feed(o,
                e("L1", StreamSide.LEFT, "k", 10),
                e("L2", StreamSide.LEFT, "k", 11),
                e("R1", StreamSide.RIGHT, "k", 10),
                e("R2", StreamSide.RIGHT, "k", 11),
                e("R3", StreamSide.RIGHT, "k", 12));
        // L10: R10,R11,R12; L11: R11,R12 (R10 diff -1 下界 0 排除)
        assertEquals(5, out.size(), "expected 5 pairs in interval");
        assertNotNull(onlyPair(out, "L1", "R1"), "L1-R1");
        assertNotNull(onlyPair(out, "L1", "R2"), "L1-R2");
        assertNotNull(onlyPair(out, "L1", "R3"), "L1-R3");
        assertNotNull(onlyPair(out, "L2", "R2"), "L2-R2");
        assertNotNull(onlyPair(out, "L2", "R3"), "L2-R3");
    }

    // ----------------------------------------------------------------
    // 缓冲上限
    // ----------------------------------------------------------------

    @Test
    public void capacityLimitTriggersWhenSideStalls() {
        JoinConfig cfg = JoinConfig.builder().lowerBound(0L).upperBound(100L)
                .maxBufferedPerSide(2).build();
        IntervalJoinOperator o = new IntervalJoinOperator(cfg);
        o.processEvent(e("L1", StreamSide.LEFT, "k", 1), null);
        o.processEvent(e("L2", StreamSide.LEFT, "k", 2), null);
        BufferCapacityExceededException ex = assertThrows(
                BufferCapacityExceededException.class,
                () -> o.processEvent(e("L3", StreamSide.LEFT, "k", 3), null),
                "third buffered left event must fail");
        assertEquals(StreamSide.LEFT, ex.side(), "exception names side");
        assertEquals(2, o.bufferedCount(StreamSide.LEFT), "buffer remains bounded");

        // 推进右水位线释放空间后，又可以进入
        o.processWatermark(StreamSide.RIGHT, 200);
        assertEquals(0, o.bufferedCount(StreamSide.LEFT), "right wm drains left buffer");
        List<JoinPair> after = new ArrayList<>();
        o.processEvent(e("L9", StreamSide.LEFT, "k", 150), after::add);
        assertEquals(1, o.bufferedCount(StreamSide.LEFT), "buffer usable after drain");
    }

    @Test
    public void capacityIsPerKeyIndependent() {
        JoinConfig cfg = JoinConfig.builder().lowerBound(0L).upperBound(100L)
                .maxBufferedPerSide(1).build();
        IntervalJoinOperator o = new IntervalJoinOperator(cfg);
        o.processEvent(e("L1", StreamSide.LEFT, "a", 1), null);
        o.processEvent(e("L1", StreamSide.LEFT, "b", 1), null); // 不同 key 各有 1 个名额
        assertEquals(2, o.bufferedCount(StreamSide.LEFT), "cap applies per key/state");
    }

    @Test
    public void emptyKeyStatesAreRemoved() {
        IntervalJoinOperator o = op(0L, 5L);
        feed(o, e("L1", StreamSide.LEFT, "k", 10), e("R1", StreamSide.RIGHT, "k", 10));
        o.processWatermark(StreamSide.LEFT, 20);
        o.processWatermark(StreamSide.RIGHT, 16);
        assertEquals(0, o.bufferedCount(StreamSide.LEFT), "left drained");
        assertEquals(0, o.bufferedCount(StreamSide.RIGHT), "right drained");
        // 两侧水位线都超过事件时间戳且缓冲为空后，去重记忆也被淘汰，键状态移除
        o.processWatermark(StreamSide.RIGHT, 20);
        o.processWatermark(StreamSide.LEFT, 21);
        assertEquals(0, o.keyCount(), "empty keyed state removed after seen pruned");
    }

    @Test
    public void metricsTrackReceivedAndEmitted() {
        IntervalJoinOperator o = op(-1L, 1L);
        feed(o,
                e("L1", StreamSide.LEFT, "k", 10),
                e("R1", StreamSide.RIGHT, "k", 10),
                e("R2", StreamSide.RIGHT, "k", 20));
        assertEquals(1L, o.metrics().leftReceived(), "left received");
        assertEquals(2L, o.metrics().rightReceived(), "right received");
        assertEquals(1L, o.metrics().emitted(), "one pair emitted");
    }

    @Test
    public void negativeTimestampsAndLongEdgesBehave() {
        IntervalJoinOperator o = op(-3L, 3L);
        List<JoinPair> out = feed(o,
                e("L", StreamSide.LEFT, "k", -5),
                e("R", StreamSide.RIGHT, "k", -8)); // diff -3 下界
        assertEquals(1, out.size(), "negative timestamps + lower edge match");

        // 极值：区间 [-MAX_VALUE, +MAX_VALUE]
        IntervalJoinOperator o2 = op(-Long.MAX_VALUE, Long.MAX_VALUE);
        List<JoinPair> hit = feed(o2,
                e("Lmin", StreamSide.LEFT, "k", Long.MIN_VALUE),
                e("Rup", StreamSide.RIGHT, "k", -1L)); // diff = MAX_VALUE，恰在上界
        assertEquals(1, hit.size(), "MIN_VALUE -> -1 diff=MAX_VALUE matches at upper edge");

        // 超出最大可达差值的配对不匹配，且不抛异常
        List<JoinPair> miss = feed(o2,
                e("Rfar", StreamSide.RIGHT, "k", Long.MAX_VALUE)); // diff = 2*MAX+1
        assertTrue(miss.isEmpty(), "MIN_VALUE -> MAX_VALUE exceeds even MAX_VALUE bound");

        // 另一侧方向的极值下界命中：L=1, R=MIN+2 → diff = -MAX_VALUE 恰在下界
        List<JoinPair> hit2 = feed(o2,
                e("Ldn", StreamSide.LEFT, "k2", 1L),
                e("Redge", StreamSide.RIGHT, "k2", Long.MIN_VALUE + 2L));
        assertEquals(1, hit2.size(), "1 -> MIN+2 diff=-MAX_VALUE matches at lower edge");

        // 再越界一格则不匹配，且全程不抛异常
        List<JoinPair> miss2 = feed(o2,
                e("L0", StreamSide.LEFT, "k3", 0L),
                e("Rmin", StreamSide.RIGHT, "k3", Long.MIN_VALUE)); // diff = MIN_VALUE < -MAX
        assertTrue(miss2.isEmpty(), "0 -> MIN_VALUE is one below lower edge, no match");
    }
}
