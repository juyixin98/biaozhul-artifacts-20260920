package com.example.itasset.web.dto;

import java.math.BigDecimal;

/**
 * One row of the depreciation explanation: how a period went from opening NBV
 * to closing NBV. {@code booked} distinguishes posted entries from projected ones.
 */
public record DepreciationLine(
        String period,
        String method,
        BigDecimal openingNbv,
        BigDecimal charge,
        BigDecimal closingNbv,
        boolean eligible,
        boolean booked,
        String note
) {
}
