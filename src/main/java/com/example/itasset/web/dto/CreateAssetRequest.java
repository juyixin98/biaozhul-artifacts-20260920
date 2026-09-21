package com.example.itasset.web.dto;

import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

import java.math.BigDecimal;

public record CreateAssetRequest(
        @NotBlank @Size(max = 40) String assetCode,
        @NotBlank @Size(max = 200) String name,
        @NotBlank @Size(max = 100) String department,
        @NotNull @DecimalMin(value = "0.0", inclusive = true) BigDecimal cost,
        @NotNull @DecimalMin(value = "0.0", inclusive = true) BigDecimal salvageValue,
        @NotNull @Pattern(regexp = "SL|DDB") String method
) {
}
