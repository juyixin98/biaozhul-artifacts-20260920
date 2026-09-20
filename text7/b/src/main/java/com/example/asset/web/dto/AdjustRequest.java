package com.example.asset.web.dto;

import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

import java.math.BigDecimal;

public record AdjustRequest(
        @DecimalMin("0.01") BigDecimal newCost,
        @Positive Integer newUsefulLifeMonths,
        @NotBlank @Size(max = 512) String reason
) {
}
