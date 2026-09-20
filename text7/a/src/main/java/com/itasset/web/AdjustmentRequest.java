package com.itasset.web;

import com.itasset.domain.DepreciationMethod;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;

import java.math.BigDecimal;

public record AdjustmentRequest(
        @NotBlank @Pattern(regexp = "\\d{6}") String effectivePeriod,
        BigDecimal newPurchaseCost,
        BigDecimal newSalvageValue,
        Integer newUsefulLifeMonths,
        DepreciationMethod newDepreciationMethod,
        BigDecimal newDecliningRatePct,
        @NotBlank String reason,
        @NotBlank String requestId) {
}
