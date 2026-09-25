package streammatch.model;

/**
 * 时间语义模式。
 * <ul>
 *   <li>{@link #EVENT_TIME}：以事件自带的 timestamp 为准；按 watermark 判定超时与迟到。</li>
 *   <li>{@link #PROCESSING_TIME}：以可注入时钟的到达时间为准（事件 timestamp 字段忽略）；
 *       超时由可注入调度器的定时器驱动。</li>
 * </ul>
 */
public enum EngineMode {
    EVENT_TIME,
    PROCESSING_TIME
}
