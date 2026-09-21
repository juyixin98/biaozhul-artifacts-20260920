package com.example.itasset.web.dto;

import com.example.itasset.domain.AssetAdjustment;

import java.math.BigDecimal;
import java.time.LocalDateTime;

public record AdjustmentResponse(
        Long id,
        Long assetId,
        String requestId,
        String effectivePeriod,
        String reason,
        BigDecimal oldCost,
        BigDecimal newCost,
        BigDecimal oldSalvageValue,
        BigDecimal newSalvageValue,
        int oldUsefulLifeMonths,
        int newUsefulLifeMonths,
        String oldMethod,
        String newMethod,
        BigDecimal oldSlMonthly,
        BigDecimal newSlMonthly,
        BigDecimal oldDdbRate,
        BigDecimal newDdbRate,
        int openEntriesDeleted,
        LocalDateTime createdAt
) {
    public static AdjustmentResponse of(AssetAdjustment a) {
        return new AdjustmentResponse(
                a.getId(),
                a.getAssetId(),
                a.getRequestId(),
                a.getEffectivePeriod(),
                a.getReason(),
                a.getOldCost(),
                a.getNewCost(),
                a.getOldSalvageValue(),
                a.getNewSalvageValue(),
                a.getOldUsefulLifeMonths(),
                a.getNewUsefulLifeMonths(),
                a.getOldMethod(),
                a.getNewMethod(),
                a.getOldSlMonthly(),
                a.getNewSlMonthly(),
                a.getOldDdbRate(),
                a.getNewDdbRate(),
                a.getOpenEntriesDeleted(),
                a.getCreatedAt());
    }
}
