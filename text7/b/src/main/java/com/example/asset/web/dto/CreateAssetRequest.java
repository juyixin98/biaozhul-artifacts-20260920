package com.example.asset.web.dto;

import com.example.asset.domain.DepreciationMethod;
import jakarta.validation.constraints.*;

import java.math.BigDecimal;
import java.time.LocalDate;

public record CreateAssetRequest(
        @NotBlank @Size(max = 64) String assetCode,
        @NotBlank @Size(max = 128) String name,
        @NotNull @DecimalMin("0.01") BigDecimal purchaseCost,
        @NotNull @DecimalMin("0.00") BigDecimal salvageValue,
        @NotNull LocalDate commissionDate,
        @Min(1) @Max(600) int usefulLifeMonths,
        @NotBlank @Size(max = 64) String department,
        @NotNull DepreciationMethod depreciationMethod
) {
}
