package bitemporal.model;

import java.time.Instant;

/**
 * 事务内的单个变更请求。
 *
 * @param op        操作类型：{@code insert}（默认）或 {@code revise}
 * @param recordId  业务记录标识
 * @param data      该有效时间段内记录的负载（不透明字符串，如键值对）
 * @param validFrom 业务有效时间起点（包含）
 * @param validTo   业务有效时间终点（排除），{@code null} 表示至今
 */
public record ChangeRequest(
        String op,
        String recordId,
        String data,
        Instant validFrom,
        Instant validTo) {

    public String normalizedOp() {
        return (op == null || op.isBlank()) ? "insert" : op.trim().toLowerCase(java.util.Locale.ROOT);
    }
}
