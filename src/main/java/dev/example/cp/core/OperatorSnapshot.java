package dev.example.cp.core;

import java.util.Map;

/**
 * 算子状态快照的不透明载体。
 *
 * <p>本参考实现的“小数据精确”快照即完整的 keyed 聚合表（{@code Map<String,Long>}）
 * 加上该算子已经消费过的输入条数 {@code processedCount}。
 * 恢复后从 {@code lastConsumedOffset+1} 开始重放，processedCount 用于内部断言。
 */
public record OperatorSnapshot(Map<String, Long> sums, long processedCount) {
}
