package com.example.asset.web.dto;

import com.example.asset.domain.DepreciationEntry;
import com.example.asset.domain.DepreciationMethod;

import java.math.BigDecimal;

/**
 * 折旧明细视图：期初 = 上期期末 + adjustmentDelta（未入账成本调整），
 * 期末 = 期初 - 本期计提。可直接解释每一期的计算过程。
 */
public record DepreciationEntryResponse(
        Long id,
        Long assetId,
        String period,
        BigDecimal openingValue,
        BigDecimal adjustmentDelta,
        BigDecimal amount,
        BigDecimal closingValue,
        DepreciationMethod method
) {
    public static DepreciationEntryResponse of(DepreciationEntry e) {
        return new DepreciationEntryResponse(e.getId(), e.getAssetId(), e.getPeriod(),
                e.getOpeningValue(), e.getAdjustmentDelta(), e.getAmount(), e.getClosingValue(), e.getMethod());
    }
}
