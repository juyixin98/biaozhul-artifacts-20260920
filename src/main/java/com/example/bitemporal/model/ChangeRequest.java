package com.example.bitemporal.model;

import java.time.LocalDate;

/**
 * 一次双时态写入请求。
 *
 * @param entityId   目标业务实体
 * @param department 新部门
 * @param role       新岗位
 * @param validFrom  业务有效区间起点（含）
 * @param validTo    业务有效区间终点（不含）；{@code null} 表示开放结尾
 * @param mode       INSERT（重叠拒绝）或 CORRECTION（追溯重述）
 */
public record ChangeRequest(
        String entityId,
        String department,
        String role,
        LocalDate validFrom,
        LocalDate validTo,
        WriteMode mode) {

    public Interval validInterval() {
        return validTo == null ? Interval.openEnded(validFrom) : Interval.of(validFrom, validTo);
    }
}
