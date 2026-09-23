package com.example.iview.view;

import java.math.BigDecimal;

/**
 * Mutable per-category aggregate. Guarded by the MaterializedView monitor;
 * not exposed outside the view (snapshots use {@link CategoryAggregate}).
 */
final class MutableAggregate {
    long qty;
    BigDecimal amount = BigDecimal.ZERO.setScale(2);

    CategoryAggregate snapshot() {
        return new CategoryAggregate(qty, amount);
    }
}
