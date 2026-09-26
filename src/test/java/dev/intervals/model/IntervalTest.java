package dev.intervals.model;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class IntervalTest {

    @Test
    @DisplayName("拒绝反向区间：lower > upper")
    void rejectsReverse() {
        IllegalArgumentException ex = assertThrows(IllegalArgumentException.class,
                () -> Interval.of(Endpoint.finite(5), Edge.CLOSED,
                        Endpoint.finite(2), Edge.CLOSED));
        assertTrue(ex.getMessage().contains("反向区间"));
    }

    @Test
    @DisplayName("拒绝退化空区间：同端点但非双闭，(x,x) / [x,x) / (x,x]")
    void rejectsDegenerate() {
        assertThrows(IllegalArgumentException.class,
                () -> Interval.of(Endpoint.finite(3), Edge.OPEN,
                        Endpoint.finite(3), Edge.OPEN));
        assertThrows(IllegalArgumentException.class,
                () -> Interval.of(Endpoint.finite(3), Edge.CLOSED,
                        Endpoint.finite(3), Edge.OPEN));
        assertThrows(IllegalArgumentException.class,
                () -> Interval.of(Endpoint.finite(3), Edge.OPEN,
                        Endpoint.finite(3), Edge.CLOSED));
    }

    @Test
    @DisplayName("单点 [x,x] 合法且被识别为 singleton")
    void singletonValid() {
        Interval s = Interval.singleton(Endpoint.finite(7));
        assertTrue(s.isSingleton());
        assertTrue(s.contains(Endpoint.finite(7)));
        assertFalse(s.contains(Endpoint.finite(6)));
        assertFalse(s.contains(Endpoint.finite(8)));
    }

    @Test
    @DisplayName("无穷端点的闭标志归一化为开")
    void infiniteEdgeNormalizedToOpen() {
        Interval i = Interval.of(Endpoint.negInf(), Edge.CLOSED,
                Endpoint.posInf(), Edge.CLOSED);
        assertEquals(Edge.OPEN, i.leftEdge());
        assertEquals(Edge.OPEN, i.rightEdge());
    }

    @Test
    @DisplayName("拒绝退化的无穷空区间 (-inf,-inf)")
    void rejectsInfiniteDegenerate() {
        assertThrows(IllegalArgumentException.class,
                () -> Interval.of(Endpoint.negInf(), Edge.OPEN,
                        Endpoint.negInf(), Edge.OPEN));
    }

    @Test
    @DisplayName("开闭端点成员关系")
    void containsRespectsEdges() {
        Interval i = Interval.of(Endpoint.finite(1), Edge.OPEN,
                Endpoint.finite(4), Edge.CLOSED);
        assertFalse(i.contains(Endpoint.finite(1)));
        assertTrue(i.contains(Endpoint.finite(2)));
        assertTrue(i.contains(Endpoint.finite(4)));
        assertFalse(i.contains(Endpoint.finite(5)));
    }

    @Test
    @DisplayName("Endpoint 非法 infinity 标志被拒绝")
    void rejectsBadInfinityFlag() {
        assertThrows(IllegalArgumentException.class,
                () -> new Endpoint(1, 5));
    }
}
