package com.winquant;

import java.util.HashMap;
import java.util.Map;
import java.util.NavigableMap;
import java.util.OptionalDouble;
import java.util.TreeMap;

/**
 * 事件时间滑动窗口上的精确分位数。
 *
 * <h2>窗口语义</h2>
 * 窗口为左开右闭区间 {@code (now - windowSize, now]}，其中 {@code now} 来自注入的 {@link Clock}。
 * <ul>
 *   <li>事件时间戳 {@code ts <= now - windowSize} 的事件被过期删除，其计数从统计结构中正确移除；</li>
 *   <li>时间戳在未来（{@code ts > now}）的事件被保留但不计入窗口，时钟推进越过其时间戳后自动进入窗口；</li>
 *   <li>过期与新进窗口的维护在每次 add / 查询 / {@link #refresh()} 时惰性、确定性地完成，
 *       不依赖后台线程——调度由此被注入到调用方；</li>
 *   <li>时钟回退（now 小于上次观测值）抛出 {@link IllegalStateException}。</li>
 * </ul>
 *
 * <h2>迟到事件</h2>
 * 到达时 {@code ts <= now - windowSize} 的事件已落在窗口之外，直接丢弃并计入 {@link #lateEventCount()}。
 *
 * <h2>精确性</h2>
 * 内部用「值 → 出现次数」的有序映射维护窗口内全部事件的精确计数，
 * 分位数按 {@link Quantiles} 的定义在计数上求精确秩，不使用任何近似草图。
 *
 * <p>非线程安全；多线程写入时请在外部同步（内置 HTTP 服务已在服务层加锁串行化访问）。</p>
 */
public final class SlidingWindowQuantile {

    private final long windowSize;
    private final Clock clock;

    /** 活跃事件按时间戳组织：ts -> (value -> count)，ts 均满足 now-windowSize < ts <= now。 */
    private final NavigableMap<Long, Map<Long, Integer>> activeByTime = new TreeMap<>();
    /** 未来事件：ts -> (value -> count)，时钟推进后迁入 activeByTime。 */
    private final NavigableMap<Long, Map<Long, Integer>> futureByTime = new TreeMap<>();
    /** 当前窗口内（now-windowSize, now]）的精确计数：value -> count。 */
    private final NavigableMap<Long, Long> valueCounts = new TreeMap<>();

    private long totalCount;
    private long lateEvents;
    private long lastNow;
    private boolean initialized;

    /**
     * @param windowSize 窗口长度（与事件时间戳同单位），必须为正
     * @param clock      注入的时间源
     */
    public SlidingWindowQuantile(long windowSize, Clock clock) {
        if (windowSize <= 0) {
            throw new IllegalArgumentException("windowSize must be positive, got " + windowSize);
        }
        this.windowSize = windowSize;
        this.clock = clock;
    }

    /**
     * 加入一个事件。
     *
     * @param timestamp 事件时间戳
     * @param value     整数值
     * @return true 表示被接受（进入窗口或暂存为未来事件）；false 表示因迟到被丢弃
     */
    public boolean add(long timestamp, long value) {
        refresh();
        if (timestamp <= lastNow - windowSize) {
            lateEvents++;
            return false;
        }
        NavigableMap<Long, Map<Long, Integer>> target =
                (timestamp <= lastNow) ? activeByTime : futureByTime;
        target.computeIfAbsent(timestamp, k -> new HashMap<>()).merge(value, 1, Integer::sum);
        if (timestamp <= lastNow) {
            valueCounts.merge(value, 1L, Long::sum);
            totalCount++;
        }
        return true;
    }

    /**
     * 将窗口状态推进到时钟当前值：过期删除旧事件、激活到期的未来事件。
     * 所有查询方法内部都会调用，通常无需显式调用；显式调用可用于强制触发过期。
     */
    public void refresh() {
        long now = clock.now();
        if (initialized && now < lastNow) {
            throw new IllegalStateException("clock went backwards: " + now + " < " + lastNow);
        }
        initialized = true;

        // 1) 激活未来事件：ts <= now 的迁入活跃集合
        while (!futureByTime.isEmpty() && futureByTime.firstKey() <= now) {
            Map.Entry<Long, Map<Long, Integer>> e = futureByTime.pollFirstEntry();
            activeByTime.computeIfAbsent(e.getKey(), k -> new HashMap<>()).putAll(e.getValue());
            for (Map.Entry<Long, Integer> vc : e.getValue().entrySet()) {
                valueCounts.merge(vc.getKey(), (long) vc.getValue(), Long::sum);
                totalCount += vc.getValue();
            }
        }

        // 2) 过期删除：ts <= now - windowSize 的从活跃集合移除并扣减计数
        long cutoff = now - windowSize;
        while (!activeByTime.isEmpty() && activeByTime.firstKey() <= cutoff) {
            Map.Entry<Long, Map<Long, Integer>> e = activeByTime.pollFirstEntry();
            for (Map.Entry<Long, Integer> vc : e.getValue().entrySet()) {
                long value = vc.getKey();
                int count = vc.getValue();
                long remaining = valueCounts.get(value) - count;
                if (remaining == 0) {
                    valueCounts.remove(value);
                } else {
                    valueCounts.put(value, remaining);
                }
                totalCount -= count;
            }
        }
        lastNow = now;
    }

    /** 当前窗口内事件总数。 */
    public long count() {
        refresh();
        return totalCount;
    }

    /** 窗口是否为空。 */
    public boolean isEmpty() {
        return count() == 0;
    }

    /** 因迟到被丢弃的事件总数。 */
    public long lateEventCount() {
        refresh();
        return lateEvents;
    }

    /** 当前窗口下界（开区间端点）：now - windowSize。 */
    public long windowStart() {
        refresh();
        return lastNow - windowSize;
    }

    /** 当前观测到的时钟值。 */
    public long now() {
        refresh();
        return lastNow;
    }

    /**
     * 当前窗口的精确分位数。
     *
     * @param q 分位点，[0,1]
     * @return 精确分位数值；窗口为空时为 {@link OptionalDouble#empty()}
     */
    public OptionalDouble quantile(double q) {
        if (q < 0.0 || q > 1.0 || Double.isNaN(q)) {
            throw new IllegalArgumentException("q must be in [0,1], got " + q);
        }
        refresh();
        if (totalCount == 0) {
            return OptionalDouble.empty();
        }
        if (totalCount == 1) {
            return OptionalDouble.of(valueCounts.firstKey());
        }
        double h = q * (totalCount - 1);
        long i = (long) Math.floor(h);
        double f = h - i;
        long lo = orderStatistic(i);
        if (f == 0.0 || i + 1 >= totalCount) {
            return OptionalDouble.of(lo);
        }
        long hi = orderStatistic(i + 1);
        return OptionalDouble.of(lo + f * ((double) hi - (double) lo));
    }

    /** 当前窗口的精确中位数；窗口为空时为 {@link OptionalDouble#empty()}。 */
    public OptionalDouble median() {
        return quantile(0.5);
    }

    /** 在精确计数上求第 index 个顺序统计量（0 起）。 */
    private long orderStatistic(long index) {
        long acc = 0;
        for (Map.Entry<Long, Long> e : valueCounts.entrySet()) {
            acc += e.getValue();
            if (index < acc) {
                return e.getKey();
            }
        }
        throw new IllegalStateException("index out of range: " + index + " >= " + totalCount);
    }
}
