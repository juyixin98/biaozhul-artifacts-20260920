package streamagg.core;

import java.math.BigDecimal;

/**
 * 最终事件账本中的一条记录：每个事件截至最终版本的真实状态。
 *
 * @param eventId  事件 ID
 * @param key      最后归属键；已撤销为 {@code null}
 * @param value    最后一次值；已撤销为 {@code null}
 * @param version  最后应用版本
 * @param active   是否存活
 */
public record LedgerEntry(String eventId, String key, BigDecimal value, long version, boolean active) {
}
