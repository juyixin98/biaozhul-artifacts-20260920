package dev.example.cp.engine;

/**
 * 一个检查点 epoch 内、对受控汇总表生效的输出负载。
 *
 * @param epochId            所属 epoch
 * @param lastConsumedOffset 该 epoch 覆盖到的输入偏移（与状态快照一致）
 * @param sumsAfter          应用本 epoch 后应达到的完整聚合表
 * @param deltas             本 epoch 内逐条输出（offset,key,sumAfter），仅作审计/演示
 */
public record OutputRecord(long epochId, long lastConsumedOffset,
                           java.util.Map<String, Long> sumsAfter,
                           java.util.List<Delta> deltas) {

    public record Delta(long offset, String key, long sumAfter) {
    }
}
