package dev.intervals.domain;

import dev.intervals.model.Edge;
import dev.intervals.model.Endpoint;
import dev.intervals.model.Interval;
import dev.intervals.model.IntervalSet;

import java.util.ArrayList;
import java.util.List;

/**
 * 区间集合代数运算：并、交、差、补。
 *
 * <p>所有运算的输入、输出均为规范化的 {@link IntervalSet}，表示唯一、稳定排序。
 */
public final class IntervalAlgebra {

    private IntervalAlgebra() {
    }

    /** 并集 A ∪ B。 */
    public static IntervalSet union(IntervalSet a, IntervalSet b) {
        List<Interval> all = new ArrayList<>(a.intervals().size() + b.intervals().size());
        all.addAll(a.intervals());
        all.addAll(b.intervals());
        return IntervalSet.of(all);
    }

    /** 交集 A ∩ B。 */
    public static IntervalSet intersection(IntervalSet a, IntervalSet b) {
        List<Interval> pieces = new ArrayList<>();
        for (Interval x : a.intervals()) {
            for (Interval y : b.intervals()) {
                Interval p = intersectOne(x, y);
                if (p != null) {
                    pieces.add(p);
                }
            }
        }
        return IntervalSet.of(pieces);
    }

    /** 差集 A − B（属于 A 但不属于 B）。 */
    public static IntervalSet difference(IntervalSet a, IntervalSet b) {
        IntervalSet rest = a;
        for (Interval sub : b.intervals()) {
            List<Interval> next = new ArrayList<>();
            for (Interval r : rest.intervals()) {
                subtractOne(r, sub, next);
            }
            rest = IntervalSet.of(next);
        }
        return rest;
    }

    /** 补集 ¬A = (−∞, +∞) − A。 */
    public static IntervalSet complement(IntervalSet a) {
        IntervalSet universe = IntervalSet.of(List.of(
                Interval.of(Endpoint.negInf(), Edge.OPEN, Endpoint.posInf(), Edge.OPEN)));
        return difference(universe, a);
    }

    /**
     * 两个区间的交集；无交集返回 {@code null}。
     */
    static Interval intersectOne(Interval x, Interval y) {
        Endpoint lo = max(x.lower(), y.lower());
        Endpoint hi = min(x.upper(), y.upper());
        if (lo.compareTo(hi) > 0) {
            return null;
        }

        Edge loEdge = andEdge(edgeAt(x, lo), edgeAt(y, lo));
        Edge hiEdge = andEdge(edgeAt(x, hi), edgeAt(y, hi));

        if (lo.compareTo(hi) == 0) {
            // 退化为一点：双方都包含该点才有交集
            return loEdge == Edge.CLOSED ? Interval.singleton(lo) : null;
        }
        return Interval.of(lo, loEdge, hi, hiEdge);
    }

    private static Endpoint min(Endpoint a, Endpoint b) {
        return a.compareTo(b) <= 0 ? a : b;
    }

    private static Endpoint max(Endpoint a, Endpoint b) {
        return a.compareTo(b) >= 0 ? a : b;
    }

    private static Edge andEdge(Edge a, Edge b) {
        return (a == Edge.CLOSED && b == Edge.CLOSED) ? Edge.CLOSED : Edge.OPEN;
    }

    /**
     * 点 p 位于区间 i 内部或边界上时，p 在该区间中的包含性：
     * 严格内部视为闭；恰在边界取该边的开/闭标志；无穷位置为开。
     */
    private static Edge edgeAt(Interval i, Endpoint p) {
        if (p.isInfinite()) {
            return Edge.OPEN;
        }
        if (p.compareTo(i.lower()) == 0) {
            return i.leftEdge();
        }
        if (p.compareTo(i.upper()) == 0) {
            return i.rightEdge();
        }
        return Edge.CLOSED;
    }

    /**
     * 计算区间 a − b，把残余的至多两个片段加入 out。
     */
    static void subtractOne(Interval a, Interval b, List<Interval> out) {
        // 不相交（含开/闭端点仅相触但不共点）时 a 原样保留
        if (intersectOne(a, b) == null) {
            out.add(a);
            return;
        }

        // 左残片：a.lower 到 b.lower 之间
        int cmpLo = b.lower().compareTo(a.lower());
        if (cmpLo > 0) {
            // b 左开则 b.lower 这一点属于 a 但不属于 b → 右闭；否则右开
            Edge right = b.lower().isFinite() && b.leftEdge() == Edge.OPEN
                    ? Edge.CLOSED : Edge.OPEN;
            out.add(Interval.of(a.lower(), a.leftEdge(), b.lower(), right));
        } else if (cmpLo == 0 && a.lower().isFinite()
                && a.leftEdge() == Edge.CLOSED && b.leftEdge() == Edge.OPEN) {
            out.add(Interval.singleton(a.lower()));
        }

        // 右残片：b.upper 到 a.upper 之间
        int cmpHi = b.upper().compareTo(a.upper());
        if (cmpHi < 0) {
            Edge left = b.upper().isFinite() && b.rightEdge() == Edge.OPEN
                    ? Edge.CLOSED : Edge.OPEN;
            out.add(Interval.of(b.upper(), left, a.upper(), a.rightEdge()));
        } else if (cmpHi == 0 && a.upper().isFinite()
                && a.rightEdge() == Edge.CLOSED && b.rightEdge() == Edge.OPEN) {
            out.add(Interval.singleton(a.upper()));
        }
    }
}
