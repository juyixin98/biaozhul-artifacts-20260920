package com.example.itasset.web.dto;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

import java.math.BigDecimal;

/**
 * Accounting adjustment. Only periods strictly after the closed boundary may be
 * affected. Open-period entries at/after {@code effectivePeriod} are reversed and the
 * new parameters apply prospectively; the old parameters and reason are preserved.
 */
public record AdjustmentRequest(
        @NotNull BigDecimal newCost,
        @NotNull BigDecimal newSalvageValue,
        @NotNull Integer newUsefulLifeMonths,
        @NotNull @Pattern(regexp = "SL|DDB") String newMethod,
        @NotNull @Pattern(regexp = "\\d{4}-\\d{2}") String effectivePeriod,
        @NotNull @Size(min = 1, max = 1000) String reason
) {
}
