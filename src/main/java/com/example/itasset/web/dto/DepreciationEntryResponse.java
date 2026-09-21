package com.example.itasset.web.dto;

import com.example.itasset.domain.DepreciationEntry;

import java.math.BigDecimal;
import java.time.LocalDateTime;

public record DepreciationEntryResponse(
        Long id,
        Long assetId,
        String assetCode,
        String period,
        String method,
        BigDecimal openingNbv,
        BigDecimal charge,
        BigDecimal closingNbv,
        LocalDateTime postedAt
) {
    public static DepreciationEntryResponse of(DepreciationEntry e, String assetCode) {
        return new DepreciationEntryResponse(
                e.getId(),
                e.getAssetId(),
                assetCode,
                e.getPeriod(),
                e.getMethod().name(),
                e.getOpeningNbv(),
                e.getCharge(),
                e.getClosingNbv(),
                e.getPostedAt());
    }
}
