package com.example.asset.web.dto;

import com.example.asset.domain.Asset;
import com.example.asset.domain.AssetStatus;
import com.example.asset.domain.DepreciationMethod;

import java.math.BigDecimal;
import java.time.LocalDate;

public record AssetResponse(
        Long id,
        String assetCode,
        String name,
        BigDecimal purchaseCost,
        BigDecimal salvageValue,
        LocalDate commissionDate,
        int usefulLifeMonths,
        String department,
        AssetStatus status,
        DepreciationMethod depreciationMethod,
        BigDecimal pendingBookValueDelta,
        long version
) {
    public static AssetResponse of(Asset a) {
        return new AssetResponse(a.getId(), a.getAssetCode(), a.getName(), a.getPurchaseCost(),
                a.getSalvageValue(), a.getCommissionDate(), a.getUsefulLifeMonths(), a.getDepartment(),
                a.getStatus(), a.getDepreciationMethod(), a.getPendingBookValueDelta(), a.getVersion());
    }
}
