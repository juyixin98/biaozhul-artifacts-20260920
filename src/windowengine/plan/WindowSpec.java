package windowengine.plan;

import java.util.List;

/**
 * 窗口规格：PARTITION BY + ORDER BY + ROWS 帧。
 * 一个查询计划里所有窗口函数共享同一个窗口规格（SQL 的 WINDOW 子句语义）。
 */
public record WindowSpec(List<String> partitionBy,
                         List<OrderKey> orderBy,
                         Frame frame) {

    public boolean isEmptyPartition() {
        return partitionBy.isEmpty();
    }

    public boolean hasOrder() {
        return !orderBy.isEmpty();
    }
}
