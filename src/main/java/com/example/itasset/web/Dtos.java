package com.example.itasset.web;

import com.example.itasset.domain.DepreciationMethod;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.Digits;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

import java.math.BigDecimal;
import java.time.LocalDate;

public final class Dtos {

    private Dtos() {
    }

    public record CreateAssetRequest(
            @NotBlank @Size(max = 64) String assetCode,
            @NotBlank @Size(max = 200) String name,
            @NotBlank @Size(max = 100) String department,
            @NotNull @DecimalMin(value = "0.01") @Digits(integer = 16, fraction = 2) BigDecimal purchaseCost,
            @NotNull @DecimalMin(value = "0.00") @Digits(integer = 16, fraction = 2) BigDecimal salvageValue,
            LocalDate placedInService,
            @NotNull @Positive Integer usefulLifeMonths,
            @NotNull DepreciationMethod depreciationMethod) {
    }

    public record TransitionRequest(
            @NotBlank @Size(max = 100) String requestId,
            @NotNull Long expectedVersion,
            @Size(max = 500) String note) {
    }

    /**
     * 折旧参数调整。所有字段可选；为 null 的字段沿用现值。effectivePeriod 必填，
     * 必须晚于最后已关账期间且该资产在生效期间尚未计提。
     */
    public record AdjustRequest(
            @NotNull Integer effectivePeriod,
            @NotBlank @Size(max = 500) String reason,
            @DecimalMin(value = "0.01") @Digits(integer = 16, fraction = 2) BigDecimal purchaseCost,
            @DecimalMin(value = "0.00") @Digits(integer = 16, fraction = 2) BigDecimal salvageValue,
            @Positive Integer usefulLifeMonths,
            DepreciationMethod depreciationMethod) {
    }

    public record PostDepreciationRequest(
            @NotNull Integer period) {
    }

    public record ClosePeriodRequest(
            @NotNull Integer period) {
    }
}
