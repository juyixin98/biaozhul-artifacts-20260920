package com.itasset.web;

import com.itasset.domain.DepreciationMethod;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.math.BigDecimal;
import java.time.LocalDate;

public record CreateAssetRequest(
        @NotBlank @Size(max = 64) String assetCode,
        @NotBlank @Size(max = 200) String name,
        @NotNull @DecimalMin(value = "0.0001") BigDecimal purchaseCost,
        @NotNull @DecimalMin(value = "0") BigDecimal salvageValue,
        @NotNull LocalDate inServiceDate,
        @NotNull Integer usefulLifeMonths,
        @NotBlank @Size(max = 100) String department,
        @NotNull DepreciationMethod depreciationMethod,
        /** 余额递减法年折旧率百分数（0,100），直线法可空。 */
        BigDecimal decliningRatePct) {
}
