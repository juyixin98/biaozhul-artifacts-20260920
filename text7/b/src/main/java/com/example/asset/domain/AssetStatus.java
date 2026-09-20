package com.example.asset.domain;

import java.util.Map;
import java.util.Set;

/**
 * 资产状态机。
 *
 * 允许的转换（处置为终态，不可恢复）：
 *   IN_STOCK     -> IN_USE
 *   IN_USE       -> UNDER_REPAIR, RETIRED
 *   UNDER_REPAIR -> IN_USE, RETIRED
 *   RETIRED      -> DISPOSED
 *   DISPOSED     -> (终态)
 */
public enum AssetStatus {
    IN_STOCK,
    IN_USE,
    UNDER_REPAIR,
    RETIRED,
    DISPOSED;

    private static final Map<AssetStatus, Set<AssetStatus>> ALLOWED = Map.of(
            IN_STOCK, Set.of(IN_USE),
            IN_USE, Set.of(UNDER_REPAIR, RETIRED),
            UNDER_REPAIR, Set.of(IN_USE, RETIRED),
            RETIRED, Set.of(DISPOSED),
            DISPOSED, Set.of()
    );

    public boolean canTransitionTo(AssetStatus target) {
        return ALLOWED.get(this).contains(target);
    }

    /** 处于这些状态的资产参与折旧计提。 */
    public static final Set<AssetStatus> DEPRECIABLE = Set.of(IN_USE, UNDER_REPAIR);
}
