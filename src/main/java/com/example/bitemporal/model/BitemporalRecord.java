package com.example.bitemporal.model;

/**
 * 双时态表中的一行物理记录（只追加，不更新、不删除）。
 *
 * <p>每个版本在双时态平面上占据一个矩形：
 * 业务有效时间 {@code valid}（{@code [validFrom, validTo)}）与
 * 系统记录时间 {@code recorded}（{@code [recordedFrom, recordedTo)}），均为半开区间。
 *
 * <p>{@code recorded.to == null} 表示该行是当前（最新）观察版本；
 * 被修正时该端被关闭为修正事务的时间戳，原行原样保留，构成历史。
 *
 * @param rowId      物理行号，单调递增
 * @param entityId   业务实体标识（成员工号）
 * @param department 部门（负载数据）
 * @param role       岗位（负载数据）
 * @param valid      业务有效时间区间（半开）
 * @param recorded   系统记录时间区间（半开）
 */
public record BitemporalRecord(
        long rowId,
        String entityId,
        String department,
        String role,
        Interval valid,
        Interval recorded) {

    public BitemporalRecord withRecordedTo(java.time.LocalDate newRecordedTo) {
        return new BitemporalRecord(
                rowId, entityId, department, role, valid,
                Interval.of(recorded.from(), newRecordedTo));
    }

    public boolean isCurrentAt(java.time.LocalDate observationDate) {
        return recorded.contains(observationDate);
    }

    public boolean isValidAt(java.time.LocalDate businessDate) {
        return valid.contains(businessDate);
    }
}
