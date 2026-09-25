package streamagg.core;

import java.util.Map;

/**
 * 对账结果：增量状态 vs 最终事件账本重算状态。
 *
 * @param consistent 是否一致
 * @param detail     不一致时的说明（null 表示一致）
 * @param incremental 增量聚合快照
 * @param recomputed  账本重算聚合快照
 */
public record ReconciliationReport(boolean consistent,
                                   String detail,
                                   Map<String, KeyStats> incremental,
                                   Map<String, KeyStats> recomputed) {
}
