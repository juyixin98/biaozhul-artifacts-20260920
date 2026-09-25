package streammatch.model;

/**
 * 迟到事件策略（仅事件时间模式；迟到 = 事件 timestamp 小于当前 watermark）。
 *
 * <ul>
 *   <li>{@link #DROP}：静默丢弃，不参与任何状态变化，结果中记为 {@code lateDropped}。
 *       已发出的匹配不撤回、不修改（无撤回语义）。这是缺省策略。</li>
 *   <li>{@link #REJECT}：整个提交请求被拒绝（服务层返回 HTTP 422），引擎状态不变。
 *       适用于“宁可失败也不静默丢数据”的调用方；批量提交时只要包含一个迟到事件即整批拒绝。</li>
 * </ul>
 *
 * 无论哪种策略，系统都提供确定性的“重放”能力：把全量事件按 {@code (timestamp, seq)}
 * 排序后重新计算，可得到无迟到假设下的理想结果，用于核对与补算（见服务 {@code /replay}）。
 */
public enum LatePolicy {
    DROP,
    REJECT
}
