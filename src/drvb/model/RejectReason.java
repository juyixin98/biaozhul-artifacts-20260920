package drvb.model;

/**
 * 事件被拒绝（未执行过滤）的原因。
 *
 * <ul>
 *   <li>{@link #MISSING_RULE_VERSION}：事件时间落在任何已发布版本区间之外，
 *       系统不会静默套用最新规则——这是本项目的核心安全保证之一；</li>
 *   <li>{@link #RECLAIMED_RULE_VERSION}：事件时间对应的历史版本已按
 *       GC 前提被回收。晚到事件仍可能到来的版本不允许回收，因此该拒绝说明
 *       事件晚到超出了保留视窗，由调用方决定如何处置。</li>
 * </ul>
 */
public enum RejectReason {
    MISSING_RULE_VERSION,
    RECLAIMED_RULE_VERSION
}
