package com.example.iview.view;

import java.math.BigDecimal;

/** Immutable per-category aggregate snapshot. */
public record CategoryAggregate(long qty, BigDecimal amount) {
}
