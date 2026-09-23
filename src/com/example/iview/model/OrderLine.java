package com.example.iview.model;

import java.math.BigDecimal;

/**
 * Order line (fact row).
 * Amount is fixed-point with scale 2 (e.g. {@code 19.90}), never floating point.
 */
public record OrderLine(String orderLineId, String productId, long qty, BigDecimal amount) {
}
