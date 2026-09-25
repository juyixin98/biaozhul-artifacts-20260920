package dev.example.cp.core;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Keyed 求和算子（精确参考实现）：维护 {@code key -> 累计和} 的完整内存表。
 *
 * <p>每来一条 {@code (key, value)} 事件，输出该 key 更新后的累计和（一条 delta 流）。
 * 全部计算为整数运算，单线程、确定性、无近似数据结构。
 */
public final class KeyedSumOperator implements Operator<KeyedSumOperator.Output> {

    /** 一条输出记录：某 key 在消费到某偏移后更新到的累计和。 */
    public record Output(long offset, String key, long sumAfter) {
    }

    private final Map<String, Long> sums = new LinkedHashMap<>();
    private long processedCount = 0L;

    @Override
    public Output process(Event event) {
        long next = sums.getOrDefault(event.key(), 0L) + event.value();
        sums.put(event.key(), next);
        processedCount++;
        return new Output(event.offset(), event.key(), next);
    }

    @Override
    public void restore(OperatorSnapshot snapshot) {
        sums.clear();
        sums.putAll(snapshot.sums());
        processedCount = snapshot.processedCount();
    }

    @Override
    public OperatorSnapshot snapshot() {
        return new OperatorSnapshot(new LinkedHashMap<>(sums), processedCount);
    }

    @Override
    public void reset() {
        sums.clear();
        processedCount = 0L;
    }

    public Map<String, Long> currentSums() {
        return new LinkedHashMap<>(sums);
    }

    public long processedCount() {
        return processedCount;
    }
}
