package streamagg.core;

/**
 * 输入日志中的一条原始记录：提交时的操作 + 引擎规范化后的版本 + 接收时间。
 * 重放整个日志必须得到与当前增量状态一致的结果。
 */
public record JournalEntry(long seq, EventOp op, long canonicalVersion, long receivedAtMillis) {
}
