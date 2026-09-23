package com.example.quantiles.window;

import com.example.quantiles.model.Event;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class SlidingWindowQuantileOperatorTest {

    private static WindowSpec spec(long size, long slide, double... qs) {
        List<Double> list = new java.util.ArrayList<>();
        for (double q : qs) {
            list.add(q);
        }
        return new WindowSpec(size, slide, list, 0L);
    }

    @Test
    void tumblingWindowBasicMedian() {
        var op = new SlidingWindowQuantileOperator(spec(10, 10, 0.5));
        // 窗口 [0,10): 1,2,3 -> median 2
        op.process(new Event(1, 1));
        op.process(new Event(5, 3));
        op.process(new Event(9, 2));
        var r = op.advanceWatermark(10);
        assertEquals(1, r.size());
        WindowResult w0 = r.get(0);
        assertEquals(0, w0.windowStartMillis());
        assertEquals(10, w0.windowEndMillis());
        assertEquals(3, w0.count());
        assertEquals("2", w0.values().get(0).toCanonicalString());
        assertEquals(0, op.retainedPanes(), "触发后 pane 应被清理");
    }

    @Test
    void slidingWindowSharesPanesAndRecomputesCorrectly() {
        var op = new SlidingWindowQuantileOperator(spec(10, 5, 0.5));
        // pane [0,5): 值 1,3；pane [5,10): 值 10,20
        op.process(new Event(1, 1));
        op.process(new Event(4, 3));
        op.process(new Event(6, 10));
        op.process(new Event(9, 20));
        // 首窗序列从首个 pane(起点0) 能落入的最早窗口 [-5,5) 开始。
        // watermark=15 触发：[-5,5)、[0,10)、[5,15)
        var r = op.advanceWatermark(15);
        assertEquals(3, r.size());

        WindowResult wm5 = r.get(0);
        assertEquals(-5, wm5.windowStartMillis());
        assertEquals(5, wm5.windowEndMillis());
        assertEquals(2, wm5.count());
        assertEquals("2", wm5.values().get(0).toCanonicalString()); // [1,3]

        WindowResult w010 = r.get(1);
        assertEquals(0, w010.windowStartMillis());
        assertEquals(4, w010.count());
        // sorted [1,3,10,20] median 6.5
        assertEquals("6.5", w010.values().get(0).toCanonicalString());

        WindowResult w515 = r.get(2);
        assertEquals(5, w515.windowStartMillis());
        assertEquals(2, w515.count());
        assertEquals("15", w515.values().get(0).toCanonicalString());

        // [-5,5) 触发后删除 pane[0]；[5,15) 触发后删除 pane[5]，无保留 pane
        assertEquals(0, op.retainedPanes());
        assertEquals(0, op.retainedEventCount());
    }

    @Test
    void multipleQuantilesEmittedInOrder() {
        var op = new SlidingWindowQuantileOperator(spec(10, 10, 0.0, 0.5, 1.0));
        op.process(new Event(0, -50));
        op.process(new Event(1, 0));
        op.process(new Event(2, 50));
        var r = op.advanceWatermark(10);
        var values = r.get(0).values();
        assertEquals("-50", values.get(0).toCanonicalString());
        assertEquals("0", values.get(1).toCanonicalString());
        assertEquals("50", values.get(2).toCanonicalString());
    }

    @Test
    void negativeTimestampsAlignToGridCorrectly() {
        // floorDiv 网格：slide=10 时 -15 落在 pane 起点 -20
        var op = new SlidingWindowQuantileOperator(spec(10, 10, 0.5));
        op.process(new Event(-15, 100));
        op.process(new Event(-11, 200));
        var r = op.advanceWatermark(0); // end=-10 窗口已到期
        WindowResult w = r.get(0);
        assertEquals(-20, w.windowStartMillis());
        assertEquals(-10, w.windowEndMillis());
        assertEquals(2, w.count());
        assertEquals("150", w.values().get(0).toCanonicalString());
    }

    @Test
    void sameTimestampEventsAllBelongToSamePanes() {
        var op = new SlidingWindowQuantileOperator(spec(10, 5, 0.5));
        for (int i = 0; i < 4; i++) {
            op.process(new Event(7, 11));
        }
        // pane[5] 所属窗口 [0,10)、[5,15)；首窗序列从 [0,10) 开始
        var r = op.advanceWatermark(15);
        assertEquals(2, r.size());
        assertEquals(4, r.get(0).count());
        assertEquals("11", r.get(0).values().get(0).toCanonicalString());
        assertEquals(4, r.get(1).count());
        assertEquals("11", r.get(1).values().get(0).toCanonicalString());
    }

    @Test
    void emptyWindowsBetweenDataAreFilledOnFlush() {
        var op = new SlidingWindowQuantileOperator(spec(10, 10, 0.5));
        op.process(new Event(1, 5));
        op.process(new Event(35, 9));
        // flush：首窗 [0,10) 与末窗 [30,40) 之间的空窗口 [10,20)、[20,30) 也要给出
        var r = op.advanceWatermark(Long.MAX_VALUE);
        assertEquals(4, r.size());
        assertEquals(List.of(0L, 10L, 20L, 30L),
                r.stream().map(WindowResult::windowStartMillis).toList());
        assertEquals(1, r.get(0).count());
        assertTrue(r.get(1).isEmpty());
        assertTrue(r.get(2).isEmpty());
        assertEquals(1, r.get(3).count());
        for (int i : new int[] {1, 2}) {
            assertEquals(1, r.get(i).values().size());
            org.junit.jupiter.api.Assertions.assertNull(r.get(i).values().get(0));
        }
    }

    @Test
    void lateEventsAreDroppedAndCounted() {
        var op = new SlidingWindowQuantileOperator(spec(10, 10, 0.5));
        op.process(new Event(1, 1));
        op.advanceWatermark(10); // [0,10) 触发
        boolean accepted = op.process(new Event(2, 99)); // 所属窗口已触发
        assertFalse(accepted);
        assertEquals(1, op.lateDroppedCount());
    }

    @Test
    void inOrderEventAfterWatermarkCanStillOpenLaterWindows() {
        var op = new SlidingWindowQuantileOperator(spec(10, 10, 0.5));
        op.process(new Event(1, 1));
        op.advanceWatermark(10);
        assertTrue(op.process(new Event(15, 42))); // 属于 [10,20)，未触发
        var r = op.advanceWatermark(20);
        assertEquals(1, r.size());
        assertEquals("42", r.get(0).values().get(0).toCanonicalString());
    }

    @Test
    void outOfOrderWithinAllowedLatenessIsAcceptedAndMerged() {
        var spec = new WindowSpec(10, 10, List.of(0.5), 5L);
        var op = new SlidingWindowQuantileOperator(spec);
        op.process(new Event(8, 10));
        op.advanceWatermark(8); // wm=8，窗口 end=10，10+5 > 8 未触发
        assertTrue(op.process(new Event(2, 20))); // 迟到 6ms < 容忍 5ms? end+lat=15, wm=8 -> 接收
        var r = op.advanceWatermark(15);
        assertEquals(1, r.size());
        // sorted [10,20] median 15
        assertEquals("15", r.get(0).values().get(0).toCanonicalString());
    }

    @Test
    void watermarkCannotMoveBackwards() {
        var op = new SlidingWindowQuantileOperator(spec(10, 10, 0.5));
        op.advanceWatermark(5);
        org.junit.jupiter.api.Assertions.assertThrows(IllegalArgumentException.class,
                () -> op.advanceWatermark(4));
    }

    @Test
    void noEventsProduceNoWindows() {
        var op = new SlidingWindowQuantileOperator(spec(10, 10, 0.5));
        assertTrue(op.advanceWatermark(100).isEmpty());
        assertTrue(op.advanceWatermark(Long.MAX_VALUE).isEmpty());
    }
}
