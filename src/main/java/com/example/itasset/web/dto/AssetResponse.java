package com.example.itasset.web.dto;

import com.example.itasset.domain.AssetStatus;
import com.example.itasset.domain.DepreciationMethod;
import com.example.itasset.domain.HardwareAsset;

import java.math.BigDecimal;
import java.time.LocalDate;

public record AssetResponse(
        Long id,
        String assetCode,
        String name,
        String department,
        AssetStatus status,
        BigDecimal cost,
        BigDecimal salvageValue,
        LocalDate inServiceDate,
        Integer usefulLifeMonths,
        int usedMonths,
        DepreciationMethod method,
        BigDecimal nbv,
        BigDecimal accumulatedDepreciation,
        BigDecimal slMonthly,
        BigDecimal ddbRate,
        String exitPeriod,
        String lastPostedPeriod,
        long version
) {
    public static AssetResponse of(HardwareAsset a) {
        return new AssetResponse(
                a.getId(),
                a.getAssetCode(),
                a.getName(),
                a.getDepartment(),
                a.getStatus(),
                a.getCost(),
                a.getSalvageValue(),
                a.getInServiceDate(),
                a.getUsefulLifeMonths(),
                a.getUsedMonths(),
                a.getMethod(),
                a.getNbv(),
                a.getCost().subtract(a.getNbv()),
                a.getSlMonthly(),
                a.getDdbRate(),
                a.getExitPeriod(),
                a.getLastPostedPeriod(),
                a.getVersion());
    }
}
