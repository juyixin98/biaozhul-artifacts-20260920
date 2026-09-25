package streammatch.model;

/**
 * 一次成功的模式匹配：A 之后出现 B，且二者之间（严格“期间”）没有 C。
 *
 * @param key        匹配所属分区键
 * @param aId        参与匹配的 A 事件 ID
 * @param bId        参与匹配的 B 事件 ID
 * @param aTimestamp A 的事件时间
 * @param bTimestamp B 的事件时间
 * @param aSeq       A 的到达序号（保留事件 ID 之外的内部序，便于测试核对次序）
 * @param bSeq       B 的到达序号
 * @param emitIndex  引擎内单调递增的“发出序号”（全局，跨 key），表示产出匹配的先后
 */
public record Match(String key,
                    String aId,
                    String bId,
                    long aTimestamp,
                    long bTimestamp,
                    long aSeq,
                    long bSeq,
                    long emitIndex) {
}
