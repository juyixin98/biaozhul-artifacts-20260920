package streammatch.model;

/**
 * 一个等待中的 A 被移出状态的原因，用于可观测性与测试核对。
 */
public enum RemovalReason {
    /** 事件时间超过 A.timestamp + windowMillis（watermark 推进所致），未等到合格 B。 */
    TIMEOUT,
    /** ALL_CANDIDATES 下，A 已匹配但最终超过窗口存活期（仅统计用，不影响匹配结果）。 */
    EXPIRED_AFTER_MATCH,
    /** 期间出现 C，等待被打断（ALL_CANDIDATES 仅清掉 C 之前的 A；见引擎文档）。 */
    INTERRUPTED_BY_C,
    /** SKIP_PAST_LAST 下，B 产出匹配后清空其余等待中的 A。 */
    SKIPPED_AFTER_MATCH
}
