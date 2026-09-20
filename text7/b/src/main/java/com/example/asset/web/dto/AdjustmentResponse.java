package com.example.asset.web.dto;

import com.example.asset.domain.AssetAdjustment;

import java.math.BigDecimal;
import java.time.Instant;

public record AdjustmentResponse(
        Long id,
        Long assetId,
        BigDecimal oldCost,
        BigDecimal newCost,
        int oldUsefulLifeMonths,
        int newUsefulLifeMonths,
        BigDecimal bookValueDelta,
        String reason,
        String actor,
        Instant createdAt
) {
    public static AdjustmentResponse of(AssetAdjustment a) {
        return new AdjustmentResponse(a.getId(), a.getAssetId(), a.getOldCost(), a.getNewCost(),
                a.getOldUsefulLifeMonths(), a.getNewUsefulLifeMonths(), a.getBookValueDelta(),
                a.getReason(), a.getActor(), a.getCreatedAt());
    }
}
