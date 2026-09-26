package com.example.bitemporal.model;

import java.time.LocalDate;

/**
 * 半开区间 [from, to)：from 为闭端（包含），to 为开端（不包含）。
 * {@code to} 为 {@code null} 表示正无穷（至今/未结束）。
 * 不可变值类型。
 */
public record Interval(LocalDate from, LocalDate to) {

    public Interval {
        if (from == null) {
            throw new IllegalArgumentException("interval start (from) must not be null");
        }
        if (to != null && !to.isAfter(from)) {
            throw new IllegalArgumentException(
                    "half-open interval requires to > from: [" + from + ", " + to + ")");
        }
    }

    public static Interval of(LocalDate from, LocalDate to) {
        return new Interval(from, to);
    }

    /** 开放结尾：[from, +inf)。 */
    public static Interval openEnded(LocalDate from) {
        return new Interval(from, null);
    }

    public boolean contains(LocalDate d) {
        return !d.isBefore(from) && (to == null || d.isBefore(to));
    }

    /** 两个半开区间是否有非零长度的重叠（仅端点相接不算重叠）。 */
    public boolean overlaps(Interval other) {
        LocalDate laterStart = from.isAfter(other.from) ? from : other.from;
        LocalDate earlierEnd = minEnd(to, other.to);
        return earlierEnd == null || laterStart.isBefore(earlierEnd);
    }

    /** 交集长度非零时返回交集，否则返回 null（相接或不相交）。 */
    public Interval intersection(Interval other) {
        LocalDate laterStart = from.isAfter(other.from) ? from : other.from;
        LocalDate earlierEnd = minEnd(to, other.to);
        if (earlierEnd != null && !laterStart.isBefore(earlierEnd)) {
            return null;
        }
        return new Interval(laterStart, earlierEnd);
    }

    private static LocalDate minEnd(LocalDate a, LocalDate b) {
        if (a == null) {
            return b;
        }
        if (b == null) {
            return a;
        }
        return a.isBefore(b) ? a : b;
    }

    @Override
    public String toString() {
        return "[" + from + ", " + (to == null ? "+inf" : to.toString()) + ")";
    }
}
