package com.example.quantiles.window;

import com.example.quantiles.model.Event;
import com.example.quantiles.quantile.Fraction;
import com.example.quantiles.quantile.SortingReferenceAccumulator;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class TrailingWindowQuantilesTest {

    private TrailingWindowQuantiles w(long size) {
        return new TrailingWindowQuantiles(size);
    }

    @Test
    void emptyWindowReturnsNullQuantileAndZeroCount() {
        var win = w(10);
        win.advanceTo(100);
        assertTrue(win.isEmpty());
        assertEquals(0, win.count());
        assertNull(win.quantile(0.5));
    }

    @Test
    void expiryKeepsAccumulatorCountConsistent() {
        var win = w(10); // (now-10, now]
        long expected = 0;
        for (long t = 0; t <= 20; t++) {
            assertTrue(win.add(new Event(t, t)));
            expected++;
            int evicted = win.advanceTo(t);
            // t=10 时 t=0 过期；此后每拍过期一个
            if (t >= 10) {
                assertEquals(1, evicted, "t=" + t + " 应恰好过期 1 条");
                expected -= evicted;
            } else {
                assertEquals(0, evicted);
            }
            assertEquals(expected, win.count(), "t=" + t + " 计数与累积器不一致");
        }
        // 最终窗口 (10,20] = {11..20}
        assertEquals(10, win.count());
        assertEquals("15.5", win.quantile(0.5).toCanonicalString());
    }

    @Test
    void duplicateValuesExpireIndividuallyAndMaintainCount() {
        var win = w(5);
        // 同一时间戳 3 个相同值
        assertTrue(win.add(new Event(1, 7)));
        assertTrue(win.add(new Event(1, 7)));
        assertTrue(win.add(new Event(1, 7)));
        win.advanceTo(1);
        assertEquals(3, win.count());
        assertEquals("7", win.quantile(0.5).toCanonicalString());

        // 推进到 t=6：cutoff=1，时间戳 1 的事件全部过期
        int evicted = win.advanceTo(6);
        assertEquals(3, evicted);
        assertEquals(0, win.count());
        assertNull(win.quantile(0.5));
    }

    @Test
    void sameTimestampEventsExitTogetherAtBoundary() {
        var win = w(4); // (now-4, now]
        win.add(new Event(2, 10));
        win.add(new Event(2, 20));
        win.advanceTo(5); // cutoff=1，仍保留
        assertEquals(2, win.count());
        win.advanceTo(6); // cutoff=2，时间戳 2 退出
        assertEquals(0, win.count());
    }

    @Test
    void negativeValuesProduceCorrectMedian() {
        var win = w(100);
        long[] vals = {-10, -4, -1, 0, 3};
        for (int i = 0; i < vals.length; i++) {
            win.add(new Event(i, vals[i]));
        }
        win.advanceTo(vals.length);
        assertEquals("-1", win.quantile(0.5).toCanonicalString());
    }

    @Test
    void eventsArrivingAlreadyExpiredAreRejected() {
        var win = w(10);
        win.advanceTo(100);
        assertFalse(win.add(new Event(90, 1))); // 90 <= 100-10
        assertEquals(0, win.count());
        assertTrue(win.add(new Event(91, 1)));   // 91 > 90，在窗口内
        assertEquals(1, win.count());
    }

    @Test
    void matchesFullSortReferenceAtEveryStep() {
        // 与“对窗口内全部值全排序”逐拍比较
        var win = w(7);
        List<Event> truth = new ArrayList<>();
        long now = 0;
        long seq = 0;
        for (long t = 0; t < 30; t++) {
            // 每拍 0~2 个事件（含相同时间戳、负值、重复值）
            int bursts = (int) (t % 3);
            for (int b = 0; b < bursts; b++) {
                long v = ((t + b) % 5) - 2; // -2..2
                Event e = new Event(t, v);
                win.add(e);
                truth.add(e);
            }
            now = t;
            win.advanceTo(now);
            long cutoff = now - 7;
            truth.removeIf(e -> e.timestampMillis() <= cutoff);

            assertEquals(truth.size(), win.count(), "t=" + t);
            if (!truth.isEmpty()) {
                List<Long> sorted = truth.stream().map(Event::value).sorted().toList();
                var ref = new SortingReferenceAccumulator();
                sorted.forEach(ref::add);
                for (double q : new double[] {0.0, 0.25, 0.5, 0.75, 1.0}) {
                    Fraction expected = ref.quantile(q);
                    Fraction actual = win.quantile(q);
                    assertNotNull(actual);
                    assertEquals(expected.toCanonicalString(), actual.toCanonicalString(),
                            "t=" + t + " q=" + q);
                }
            }
        }
    }

    @Test
    void timeCannotMoveBackwards() {
        var win = w(10);
        win.advanceTo(5);
        assertThrows(IllegalArgumentException.class, () -> win.advanceTo(4));
    }
}
