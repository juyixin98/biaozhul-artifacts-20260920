package sessions.model;

import java.util.Objects;

/**
 * 一个结果窗口（已确定的区间 [start, end] 与聚合值）。
 *
 * <p>窗口端点语义：事件 t 属于窗口当且仅当 {@code start <= t <= end}。
 * 两个"贴合"窗口（前一个的 end + gap == 后一个的 start）<b>不</b>合并，
 * 因为合并条件是事件间隔 {@code <= gap}，贴合时间隔恰好等于 gap，
 * 只有当桥接事件使得相邻事件间隔均 {@code <= gap} 时才会合并。
 */
public final class Window {
    private final long start;
    private final long end;

    public Window(long start, long end) {
        if (start > end) {
            throw new IllegalArgumentException("window start " + start + " > end " + end);
        }
        this.start = start;
        this.end = end;
    }

    public long start() {
        return start;
    }

    public long end() {
        return end;
    }

    /**
     * 单个事件 {@code t} 在间隔 {@code gap} 下能否并入本窗口：
     * 事件须落在 [start - gap, end + gap]（端点均包含）。
     */
    public boolean connects(long t, long gap) {
        return start - gap <= t && t <= end + gap;
    }

    /**
     * 两个窗口能否在间隔 gap 下合并：在一条轴上，任一窗口的端点 + gap
     * 能够触及另一窗口的闭区间。即按端点排列后
     * {@code max(start1,start2) - min(end1,end2) <= gap}。
     *
     * <p>例：[1,5] 与 [21,25]，gap=10：21-5=16&gt;10 不相连；
     * 扩张为 [1,15] 后：21-15=6&lt;=10 相连。
     */
    public boolean connects(Window other, long gap) {
        long leftEnd = Math.min(end, other.end);
        long rightStart = Math.max(start, other.start);
        return rightStart - leftEnd <= gap;
    }

    @Override
    public boolean equals(Object o) {
        if (!(o instanceof Window w)) {
            return false;
        }
        return start == w.start && end == w.end;
    }

    @Override
    public int hashCode() {
        return Objects.hash(start, end);
    }

    @Override
    public String toString() {
        return "[" + start + ", " + end + "]";
    }
}
