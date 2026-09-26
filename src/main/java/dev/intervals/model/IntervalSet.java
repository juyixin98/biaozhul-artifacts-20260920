package dev.intervals.model;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/**
 * 规范化的区间集合：互不相交、按端点稳定排序、相邻可合并区间已合并。
 *
 * <p>规范化规则（保证表示唯一）：
 * <ol>
 *   <li>丢弃空输入；</li>
 *   <li>按下端点排序（同位置闭端在前）；</li>
 *   <li>两两重叠或“闭-闭”相邻（[a,b] 与 [b,c]）时合并；开-闭相邻（(..,b) 与 [b,..]）不合并；</li>
 *   <li>合并后连接处的包含性：左闭取左区间的左边，右闭取右区间的右边。</li>
 * </ol>
 *
 * <p>不可变。
 */
public final class IntervalSet {

    private static final Comparator<Interval> ORDER =
            Comparator.comparing(Interval::lower)
                    .thenComparingInt(i -> i.leftEdge() == Edge.CLOSED ? 0 : 1);

    private final List<Interval> intervals;

    private IntervalSet(List<Interval> intervals) {
        this.intervals = Collections.unmodifiableList(intervals);
    }

    /** 由任意区间列表构造规范化集合。 */
    public static IntervalSet of(List<Interval> raw) {
        List<Interval> sorted = new ArrayList<>(raw);
        sorted.sort(ORDER);

        List<Interval> merged = new ArrayList<>();
        for (Interval cur : sorted) {
            if (merged.isEmpty()) {
                merged.add(cur);
                continue;
            }
            Interval last = merged.get(merged.size() - 1);
            if (canMerge(last, cur)) {
                merged.set(merged.size() - 1, merge(last, cur));
            } else {
                merged.add(cur);
            }
        }
        return new IntervalSet(merged);
    }

    public static IntervalSet empty() {
        return new IntervalSet(List.of());
    }

    public List<Interval> intervals() {
        return intervals;
    }

    public boolean isEmpty() {
        return intervals.isEmpty();
    }

    /**
     * 两个已排序区间是否可合并：重叠，或在同一点处相触且该点至少属于其中一方。
     * 仅当双方都排除相触点（右开 + 左开）时才保留为两段。
     */
    private static boolean canMerge(Interval a, Interval b) {
        int cmp = a.upper().compareTo(b.lower());
        if (cmp < 0) {
            return false;
        }
        if (cmp > 0) {
            return true; // 明确重叠
        }
        return a.rightEdge() == Edge.CLOSED || b.leftEdge() == Edge.CLOSED;
    }

    private static Interval merge(Interval a, Interval b) {
        // 左边界：位置更小者决定；同位置时包含性取“更闭”
        Endpoint lower;
        Edge left;
        int cmpLo = a.lower().compareTo(b.lower());
        if (cmpLo < 0) {
            lower = a.lower();
            left = a.leftEdge();
        } else if (cmpLo > 0) {
            lower = b.lower();
            left = b.leftEdge();
        } else {
            lower = a.lower();
            left = (a.leftEdge() == Edge.CLOSED || b.leftEdge() == Edge.CLOSED)
                    ? Edge.CLOSED : Edge.OPEN;
        }

        // 右边界：位置更大者决定（包含关系时取外层区间的边）；同位置取“更闭”
        Endpoint upper;
        Edge right;
        int cmpHi = a.upper().compareTo(b.upper());
        if (cmpHi > 0) {
            upper = a.upper();
            right = a.rightEdge();
        } else if (cmpHi < 0) {
            upper = b.upper();
            right = b.rightEdge();
        } else {
            upper = a.upper();
            right = (a.rightEdge() == Edge.CLOSED || b.rightEdge() == Edge.CLOSED)
                    ? Edge.CLOSED : Edge.OPEN;
        }
        return Interval.of(lower, left, upper, right);
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof IntervalSet other)) {
            return false;
        }
        return intervals.equals(other.intervals);
    }

    @Override
    public int hashCode() {
        return intervals.hashCode();
    }

    @Override
    public String toString() {
        return intervals.toString();
    }
}
