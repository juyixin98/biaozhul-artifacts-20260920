package cep.config;

/**
 * 迟到事件处理策略。判定迟到的标准是 {@code event.timestamp + allowedLateness < watermark}
 * （严格小于：恰好在边界上不算迟到）。
 *
 * <ul>
 *   <li>{@link #DROP}：丢弃，计入 droppedLate（默认，确定性的标准 watermark 语义）；</li>
 *   <li>{@link #ACCEPT}：在"迟到尽力车道"立即按到达顺序处理：可与现存有效 A/B 产生新结果，
 *       已发出的匹配/超时一律不撤回；迟到 C 仍会杀掉现存的、全序在它之前的活跃 A。
 *       迟到车道产出的结果标记 late=true。</li>
 * </ul>
 */
public enum LatePolicy {
    DROP,
    ACCEPT
}
