package com.example.itasset.web.dto;

import java.math.BigDecimal;
import java.util.List;

/**
 * Full depreciation explanation for an asset: regime parameters, eligibility window,
 * booked ledger entries (opening/charge/closing per period) and a projection of future
 * periods through the useful life.
 */
public record DepreciationExplanation(
        Long assetId,
        String assetCode,
        String status,
        String method,
        BigDecimal cost,
        BigDecimal salvageValue,
        Integer usefulLifeMonths,
        String inServicePeriod,
        String firstEligiblePeriod,
        String exitPeriod,
        String lastPostedPeriod,
        BigDecimal currentNbv,
        int usedMonthsInRegime,
        BigDecimal slMonthly,
        BigDecimal ddbRate,
        List<DepreciationLine> lines
) {
}
