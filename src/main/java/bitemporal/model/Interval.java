package bitemporal.model;

import java.time.Instant;

/**
 * 半开时间区间 [from, to)。
 *
 * <ul>
 *   <li>{@code from} 包含在内，{@code to} 排除在外；</li>
 *   <li>{@code to == null} 表示开放式右端（业务上“至今”，系统时间上“当前版本”）；</li>
 *   <li>相邻区间 [a,b) 与 [b,c) 不相交，可以无缝拼接。</li>
 * </ul>
 *
 * @param from 区间起点（包含）
 * @param to   区间终点（排除），{@code null} 表示开放至无穷
 */
public record Interval(Instant from, Instant to) {

    /** 逻辑上的无穷右端，仅用于区间比较，绝不持久化或序列化。 */
    public static Instant endOrMax(Instant end) {
        return end != null ? end : Instant.MAX;
    }

    public Interval {
        if (from == null) {
            throw new IllegalArgumentException("interval.from must not be null");
        }
        if (to != null && !to.isAfter(from)) {
            throw new IllegalArgumentException(
                    "half-open interval requires to > from, got [" + from + ", " + to + ")");
        }
    }

    public static Interval of(Instant from, Instant to) {
        return new Interval(from, to);
    }

    public static Interval startingAt(Instant from) {
        return new Interval(from, null);
    }

    /** 时刻 {@code t} 是否落在 [from, to) 内。 */
    public boolean contains(Instant t) {
        return !t.isBefore(from) && t.isBefore(endOrMax(to));
    }

    /** 两个半开区间是否存在非空交集；[a,b) 与 [b,c) 判为不相交。 */
    public boolean overlaps(Interval other) {
        return this.from.isBefore(endOrMax(other.to))
                && other.from.isBefore(endOrMax(this.to));
    }

    public Instant endOrMax() {
        return endOrMax(to);
    }
}
