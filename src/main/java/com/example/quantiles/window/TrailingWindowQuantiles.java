package com.example.quantiles.window;

import com.example.quantiles.model.Event;
import com.example.quantiles.quantile.Fraction;
import com.example.quantiles.quantile.QuantileAccumulator;
import com.example.quantiles.quantile.TreeMapAccumulator;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;

/**
 * 单个“尾随滑动窗口” {@code (now - sizeMillis, now]} 的精确分位数视图。
 *
 * <p>与 {@link SlidingWindowQuantileOperator} 的 pane 模型互补：这里显式保留每条
 * 事件的有序队列（按时间戳非降序，允许相同时间戳），在时间推进时把
 * {@code timestamp <= now - sizeMillis} 的事件逐条从精确累积器中删除，
 * 因此<b>过期删除的计数维护是可直接观察、可测试的</b>：
 * {@link #count()} 始终等于队列长度，等于累积器内总多重度。
 *
 * <p>窗口语义：{@code (now-sizeMillis, now]}，右端含、左端不含；
 * 同时间戳事件随到期整体退出。空窗口时 {@link #quantile(double)} 返回 null。
 */
public final class TrailingWindowQuantiles {

    private final long sizeMillis;
    private final QuantileAccumulator accumulator;

    /** 窗口内事件，按时间戳非降序。 */
    private final Deque<Event> window = new ArrayDeque<>();
    private long now = Long.MIN_VALUE;

    public TrailingWindowQuantiles(long sizeMillis) {
        this(sizeMillis, new TreeMapAccumulator());
    }

    public TrailingWindowQuantiles(long sizeMillis, QuantileAccumulator accumulator) {
        if (sizeMillis <= 0) {
            throw new IllegalArgumentException("sizeMillis 必须为正: " + sizeMillis);
        }
        this.sizeMillis = sizeMillis;
        this.accumulator = accumulator;
    }

    /**
     * 加入一条事件。若事件时间早于或等于当前窗口左边界（{@code now-sizeMillis}），
     * 它在到达时即已过期，返回 false 且不入窗。乱序但仍落在窗口内的事件可以接收，
     * 并按时间戳插入队列以保证队头始终为最老事件。
     */
    public boolean add(Event event) {
        if (now != Long.MIN_VALUE && event.timestampMillis() <= now - sizeMillis) {
            return false;
        }
        // ArrayDeque 不按索引插入；窗口通常不大，线性定位后用重建方式保持有序。
        // （事件流主路径是时间有序的；该类的定位是“过期删除计数正确”的清晰模型。）
        if (window.isEmpty() || window.peekLast().timestampMillis() <= event.timestampMillis()) {
            window.addLast(event);
        } else {
            Deque<Event> rebuilt = new ArrayDeque<>(window.size() + 1);
            boolean inserted = false;
            for (Event e : window) {
                if (!inserted && event.timestampMillis() < e.timestampMillis()) {
                    rebuilt.addLast(event);
                    inserted = true;
                }
                rebuilt.addLast(e);
            }
            if (!inserted) {
                rebuilt.addLast(event);
            }
            window.clear();
            rebuilt.forEach(window::addLast);
        }
        accumulator.add(event.value());
        return true;
    }

    /**
     * 把当前时间推进到 {@code newNow}（不允许倒退），删除所有
     * {@code timestamp <= newNow - sizeMillis} 的过期事件并同步从累积器移除。
     * 返回被删除的事件数。
     */
    public int advanceTo(long newNow) {
        if (now != Long.MIN_VALUE && newNow < now) {
            throw new IllegalArgumentException("时间不允许倒退: " + now + " -> " + newNow);
        }
        now = newNow;
        long cutoff = now - sizeMillis; // 保留 (cutoff, now]
        int evicted = 0;
        while (!window.isEmpty() && window.peekFirst().timestampMillis() <= cutoff) {
            Event e = window.pollFirst();
            accumulator.remove(e.value()); // 关键：过期即精确扣减计数
            evicted++;
        }
        return evicted;
    }

    public long count() {
        return accumulator.count();
    }

    public boolean isEmpty() {
        return accumulator.count() == 0;
    }

    /** 当前窗口快照（按入窗顺序），主要用于测试与“全排序”对照。 */
    public List<Event> events() {
        return List.copyOf(window);
    }

    /** 分位数；窗口为空返回 null（不抛异常，方便直接表达“窗口为空”）。 */
    public Fraction quantile(double q) {
        if (isEmpty()) {
            return null;
        }
        return accumulator.quantile(q);
    }

    public List<Fraction> quantiles(List<Double> qs) {
        if (isEmpty()) {
            List<Fraction> out = new java.util.ArrayList<>(qs.size());
            for (int i = 0; i < qs.size(); i++) {
                out.add(null);
            }
            return out;
        }
        return accumulator.quantiles(qs);
    }
}
