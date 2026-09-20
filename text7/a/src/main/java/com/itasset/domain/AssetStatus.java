package com.itasset.domain;

import java.util.EnumSet;
import java.util.Set;

/**
 * 资产状态及允许的转换规则。
 *
 * 库存 IN_STOCK -> 使用中 IN_USE / 退役 RETIRED / 处置 DISPOSED
 * 使用中 IN_USE -> 库存 / 维修 UNDER_REPAIR / 退役 / 处置
 * 维修 UNDER_REPAIR -> 使用中 / 退役 / 处置
 * 退役 RETIRED -> 处置 DISPOSED
 * 处置 DISPOSED -> （终态，不可恢复使用）
 */
public enum AssetStatus {
    IN_STOCK,
    IN_USE,
    UNDER_REPAIR,
    RETIRED,
    DISPOSED;

    private Set<AssetStatus> allowed;

    static {
        IN_STOCK.allowed = EnumSet.of(IN_USE, RETIRED, DISPOSED);
        IN_USE.allowed = EnumSet.of(IN_STOCK, UNDER_REPAIR, RETIRED, DISPOSED);
        UNDER_REPAIR.allowed = EnumSet.of(IN_USE, RETIRED, DISPOSED);
        RETIRED.allowed = EnumSet.of(DISPOSED);
        DISPOSED.allowed = EnumSet.noneOf(AssetStatus.class);
    }

    public boolean canTransitionTo(AssetStatus target) {
        return allowed.contains(target);
    }
}
