package dev.example.cp.engine;

import dev.example.cp.core.OperatorSnapshot;

/**
 * 一次已持久化检查点的全部内容：
 *
 * @param epochId              检查点（epoch）编号，单调递增
 * @param lastConsumedOffset   该检查点覆盖的最后一条输入偏移；-1 表示尚无输入被纳入
 * @param operator             算子状态完整快照
 */
public record CheckpointState(long epochId, long lastConsumedOffset, OperatorSnapshot operator) {
}
