package dev.intervals.model;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static dev.intervals.model.Edge.CLOSED;
import static dev.intervals.model.Edge.OPEN;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class IntervalSetTest {

    private static Interval iv(int lo, Edge le, int hi, Edge re) {
        return Interval.of(Endpoint.finite(lo), le, Endpoint.finite(hi), re);
    }

    @Test
    @DisplayName("闭-闭相邻 [a,b]+[b,c] 合并为 [a,c]")
    void mergeClosedTouch() {
        IntervalSet s = IntervalSet.of(List.of(iv(1, CLOSED, 3, CLOSED), iv(3, CLOSED, 5, CLOSED)));
        assertEquals(1, s.intervals().size());
        assertEquals(iv(1, CLOSED, 5, CLOSED), s.intervals().get(0));
    }

    @Test
    @DisplayName("开-开相邻 (..,3) 与 (3,..) 不合并（点 3 双方都不包含）")
    void noMergeOpenTouch() {
        IntervalSet s = IntervalSet.of(List.of(iv(1, CLOSED, 3, OPEN), iv(3, OPEN, 5, CLOSED)));
        assertEquals(2, s.intervals().size());
    }

    @Test
    @DisplayName("包含合并：外层区间吞掉内层，端点包含性取更闭者")
    void containmentMerge() {
        IntervalSet s = IntervalSet.of(List.of(
                iv(1, OPEN, 5, OPEN),
                iv(1, CLOSED, 5, CLOSED)));
        assertEquals(1, s.intervals().size());
        assertEquals(iv(1, CLOSED, 5, CLOSED), s.intervals().get(0));
    }

    @Test
    @DisplayName("无穷区间合并：(-inf,3] 与 (2,+inf) → 全集")
    void infiniteMergeToUniverse() {
        IntervalSet s = IntervalSet.of(List.of(
                Interval.of(Endpoint.negInf(), OPEN, Endpoint.finite(3), CLOSED),
                Interval.of(Endpoint.finite(2), OPEN, Endpoint.posInf(), OPEN)));
        assertEquals(1, s.intervals().size());
        Interval u = s.intervals().get(0);
        assertTrue(u.lower().isNegInf() && u.upper().isPosInf());
    }

    @Test
    @DisplayName("稳定排序：乱序输入规范化后按下端点升序")
    void stableOrder() {
        List<Interval> raw = new ArrayList<>(List.of(
                iv(9, CLOSED, 10, CLOSED),
                iv(1, OPEN, 2, OPEN),
                iv(5, CLOSED, 6, CLOSED)));
        IntervalSet s = IntervalSet.of(raw);
        assertEquals(1, s.intervals().get(0).lower().value());
        assertEquals(5, s.intervals().get(1).lower().value());
        assertEquals(9, s.intervals().get(2).lower().value());
    }

    @Test
    @DisplayName("规范化幂等：对结果再次规范化不变")
    void idempotent() {
        IntervalSet s = IntervalSet.of(List.of(iv(1, CLOSED, 4, OPEN), iv(3, CLOSED, 6, CLOSED)));
        assertEquals(s, IntervalSet.of(new ArrayList<>(s.intervals())));
    }
}
