package com.example.itasset.domain;

import java.util.EnumSet;
import java.util.Map;
import java.util.Set;

/**
 * Asset lifecycle states.
 *
 * Allowed transitions (explicit whitelist):
 * <pre>
 * IN_STOCK      -> IN_USE (activate), DISPOSED (scrap unused stock)
 * IN_USE        -> UNDER_REPAIR, RETIRED
 * UNDER_REPAIR  -> IN_USE, RETIRED
 * RETIRED       -> DISPOSED
 * DISPOSED      -> (terminal)
 * </pre>
 * RETIRED is a terminal business state for depreciation: a retired asset can never
 * return to service. DISPOSED is globally terminal and unrecoverable.
 */
public enum AssetStatus {
    IN_STOCK,
    IN_USE,
    UNDER_REPAIR,
    RETIRED,
    DISPOSED;

    private static final Map<AssetStatus, Set<AssetStatus>> ALLOWED = Map.of(
            IN_STOCK, EnumSet.of(IN_USE, DISPOSED),
            IN_USE, EnumSet.of(UNDER_REPAIR, RETIRED),
            UNDER_REPAIR, EnumSet.of(IN_USE, RETIRED),
            RETIRED, EnumSet.of(DISPOSED),
            DISPOSED, EnumSet.noneOf(AssetStatus.class)
    );

    public boolean canTransitionTo(AssetStatus target) {
        return ALLOWED.get(this).contains(target);
    }

    /** Depreciation is booked for active assets (repairs keep depreciation running). */
    public boolean isActive() {
        return this == IN_USE || this == UNDER_REPAIR;
    }
}
