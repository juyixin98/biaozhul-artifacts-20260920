package com.winquant;

import org.junit.jupiter.api.Test;

import java.util.OptionalDouble;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 滑动窗口语义：过期删除、同时间戳、重复值、负值、空窗口、未来/迟到事件。 */
class SlidingWindowQuantileTest {

    private static final long W = 10; // 窗口长度 10，区间 (now-10, now]

    @Test
    void emptyWindowHasNoQuantile() {
        ManualClock clock = new ManualClock(100);
        SlidingWindowQuantile w = new SlidingWindowQuantile(W, clock);
        assertTrue(w.isEmpty());
        assertEquals(0, w.count());
        assertTrue(w.median().isEmpty());
        assertTrue(w.quantile(0.9).isEmpty());
    }

    @Test
    void windowEmptiesAgainAfterAllEventsExpire() {
        ManualClock clock = new ManualClock(100);
        SlidingWindowQuantile w = new SlidingWindowQuantile(W, clock);
        w.add(100, 5);
        assertEquals(5.0, w.median().orElseThrow());
        clock.advanceTo(111); // (101, 111]，ts=100 过期
        assertTrue(w.isEmpty());
        assertTrue(w.median().isEmpty());
    }

    @Test
    void expiryRemovesCountsCorrectly() {
        ManualClock clock = new ManualClock(100);
        SlidingWindowQuantile w = new SlidingWindowQuantile(W, clock);
        w.add(105, 1);  // 未来事件，暂存
        w.add(100, 2);
        w.add(100, 3);
        // 当前窗口 (90,100]：{2,3}，median 2.5；ts=105 尚未进入窗口
        assertEquals(2, w.count());
        assertEquals(2.5, w.median().orElseThrow());

        clock.advanceTo(105); // (95,105]：{105:1, 100:2, 100:3}
        assertEquals(3, w.count());
        assertEquals(2.0, w.median().orElseThrow());

        clock.advanceTo(110); // (100,110]：ts=100 过期（左开区间）→ {1}
        assertEquals(1, w.count());
        assertEquals(1.0, w.median().orElseThrow());

        clock.advanceTo(114); // (104,114]：ts=105 仍在
        assertEquals(1, w.count());

        clock.advanceTo(115); // (105,115]：ts=105 也过期
        assertEquals(0, w.count());
        assertTrue(w.median().isEmpty());
    }

    @Test
    void sameTimestampEventsAllCounted() {
        ManualClock clock = new ManualClock(50);
        SlidingWindowQuantile w = new SlidingWindowQuantile(W, clock);
        for (int i = 0; i < 5; i++) {
            w.add(50, 7);
        }
        assertEquals(5, w.count());
        assertEquals(7.0, w.median().orElseThrow());
        assertEquals(7.0, w.quantile(0.0).orElseThrow());
        assertEquals(7.0, w.quantile(1.0).orElseThrow());
    }

    @Test
    void allDuplicateValues() {
        ManualClock clock = new ManualClock(10);
        SlidingWindowQuantile w = new SlidingWindowQuantile(W, clock);
        w.add(1, -4);
        w.add(2, -4);
        w.add(3, -4);
        w.add(4, -4);
        assertEquals(-4.0, w.median().orElseThrow());
        assertEquals(-4.0, w.quantile(0.25).orElseThrow());
        assertEquals(-4.0, w.quantile(0.75).orElseThrow());
    }

    @Test
    void negativeValues() {
        ManualClock clock = new ManualClock(10);
        SlidingWindowQuantile w = new SlidingWindowQuantile(W, clock);
        w.add(1, -10);
        w.add(2, -3);
        w.add(3, 0);
        w.add(4, 7);
        assertEquals(-1.5, w.median().orElseThrow());
        assertEquals(-10.0, w.quantile(0.0).orElseThrow());
        assertEquals(7.0, w.quantile(1.0).orElseThrow());
    }

    @Test
    void lateEventsAreDiscardedAndCounted() {
        ManualClock clock = new ManualClock(100);
        SlidingWindowQuantile w = new SlidingWindowQuantile(W, clock);
        w.add(100, 1);
        assertFalse(w.add(90, 2));  // 90 <= 100-10，迟到
        assertFalse(w.add(50, 3));
        assertTrue(w.add(91, 4));   // 91 > 90，在窗口内
        assertEquals(2, w.lateEventCount());
        assertEquals(2, w.count());
    }

    @Test
    void outOfOrderWithinWindowIsAccepted() {
        ManualClock clock = new ManualClock(100);
        SlidingWindowQuantile w = new SlidingWindowQuantile(W, clock);
        w.add(100, 10);
        w.add(95, 20); // 乱序但在窗口内
        w.add(98, 30);
        assertEquals(3, w.count());
        assertEquals(20.0, w.median().orElseThrow());
    }

    @Test
    void futureEventsEnterWindowAsClockAdvances() {
        ManualClock clock = new ManualClock(0);
        SlidingWindowQuantile w = new SlidingWindowQuantile(W, clock);
        w.add(5, 100); // 未来事件
        assertEquals(0, w.count());
        clock.advanceTo(4);
        assertEquals(0, w.count());
        clock.advanceTo(5);
        assertEquals(1, w.count());
        assertEquals(100.0, w.median().orElseThrow());
    }

    @Test
    void backwardsClockRejected() {
        ManualClock clock = new ManualClock(100);
        SlidingWindowQuantile w = new SlidingWindowQuantile(W, clock);
        w.add(100, 1);
        w.count();
        assertThrows(IllegalArgumentException.class, () -> clock.advanceTo(50));
    }

    @Test
    void rejectsNonPositiveWindow() {
        assertThrows(IllegalArgumentException.class, () -> new SlidingWindowQuantile(0, new ManualClock(0)));
        assertThrows(IllegalArgumentException.class, () -> new SlidingWindowQuantile(-5, new ManualClock(0)));
    }

    @Test
    void quantileMatchesDefinitionOnWindowContents() {
        ManualClock clock = new ManualClock(10);
        SlidingWindowQuantile w = new SlidingWindowQuantile(W, clock);
        w.add(1, 1);
        w.add(2, 2);
        w.add(3, 3);
        w.add(4, 4);
        OptionalDouble q25 = w.quantile(0.25);
        assertEquals(1.75, q25.orElseThrow(), 1e-12);
        assertEquals(3.25, w.quantile(0.75).orElseThrow(), 1e-12);
    }
}
