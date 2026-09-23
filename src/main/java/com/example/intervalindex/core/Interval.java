package com.example.intervalindex.core;

/**
 * 一个半开区间 [start, end)，endpoints 为整数。
 *
 * <p>半开语义：区间包含 start、不包含 end。因此：
 * <ul>
 *   <li>相邻区间 [1,5) 与 [5,9) 不重叠；</li>
 *   <li>点 t 被覆盖当且仅当 {@code start <= t < end}；</li>
 *   <li>空/逆序区间（start &gt;= end）非法。</li>
 * </ul>
 */
public record Interval(long id, long start, long end) {

    public Interval {
        if (start >= end) {
            throw new IllegalArgumentException(
                    "invalid half-open interval [" + start + ", " + end
                            + "): require start < end");
        }
    }

    /** 本区间是否与查询区间 [lo, hi) 有非空交集。 */
    public boolean overlaps(long lo, long hi) {
        return start < hi && end > lo;
    }

    /** 点 t 是否被本区间覆盖（左闭右开）。 */
    public boolean contains(long t) {
        return start <= t && t < end;
    }
}
