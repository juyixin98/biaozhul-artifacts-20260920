package com.example.itasset.service;

import com.example.itasset.domain.DepreciationMethod;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * Depreciation math and booking rules.
 *
 * <h3>Rounding</h3>
 * All money is fixed-point DECIMAL(18,2). The raw charge for every period is rounded to
 * 2 decimal places with {@link RoundingMode#HALF_UP} (四舍五入) before booking.
 *
 * <h3>Straight line (SL)</h3>
 * Monthly charge {@code (cost - salvage) / usefulLifeMonths}, rounded HALF_UP. This fixed
 * amount is stored on the asset ({@code slMonthly}) when the regime starts (activation or
 * adjustment), so every ordinary period books exactly the same charge. The final period
 * of the regime books only the remainder to bring NBV exactly to salvage value, so
 * accumulated rounding can never push NBV below salvage.
 *
 * <h3>Double declining balance (DDB)</h3>
 * Fixed monthly rate {@code 2 / usefulLifeMonths} (annual double-declining rate
 * {@code 2/yearsOfLife}, spread over 12 months), stored as {@code ddbRate}. Raw charge
 * {@code openingNbv * rate}, HALF_UP. A salvage floor caps every charge so
 * {@code closingNbv >= salvage}; once the raw charge rounds below one cent the asset
 * stays at NBV. DDB does not switch to straight line.
 */
public final class DepreciationPolicy {

    public static final int MONEY_SCALE = 2;
    public static final RoundingMode ROUNDING = RoundingMode.HALF_UP;

    private DepreciationPolicy() {
    }

    public static BigDecimal money(BigDecimal raw) {
        return raw.setScale(MONEY_SCALE, ROUNDING);
    }

    /** Straight-line monthly charge, rounded HALF_UP. */
    public static BigDecimal straightLineMonthly(BigDecimal cost, BigDecimal salvage, int usefulLifeMonths) {
        if (usefulLifeMonths <= 0) {
            throw new IllegalArgumentException("usefulLifeMonths must be positive");
        }
        return cost.subtract(salvage)
                .divide(BigDecimal.valueOf(usefulLifeMonths), MONEY_SCALE, ROUNDING);
    }

    /**
     * Fixed DDB monthly rate. With life N in months (Y = N/12 years), the annual
     * double-declining rate is 2/Y = 24/N, so the monthly rate is (24/N)/12 = 2/N.
     * Example: 48 months -> 2/48 = 0.04166667/month. Kept at 8 decimal places.
     */
    public static BigDecimal ddbMonthlyRate(int usefulLifeMonths) {
        if (usefulLifeMonths <= 0) {
            throw new IllegalArgumentException("usefulLifeMonths must be positive");
        }
        return BigDecimal.valueOf(2L)
                .divide(BigDecimal.valueOf(usefulLifeMonths), 8, RoundingMode.HALF_UP);
    }

    /**
     * Charge for one period given the opening NBV and regime parameters.
     *
     * @param remainingMonthsRegime months left in the current SL regime (for the final-period plug);
     *                              ignored for DDB
     */
    public static BigDecimal monthlyCharge(DepreciationMethod method,
                                           BigDecimal openingNbv,
                                           BigDecimal salvage,
                                           BigDecimal slMonthly,
                                           BigDecimal ddbRate,
                                           int remainingMonthsRegime) {
        BigDecimal raw;
        if (method == DepreciationMethod.SL) {
            raw = slMonthly;
            if (remainingMonthsRegime <= 1) {
                // Final period: book exactly what is left to salvage, never more.
                raw = openingNbv.subtract(salvage);
            }
        } else {
            raw = openingNbv.multiply(ddbRate);
        }
        BigDecimal charge = money(raw);
        BigDecimal maxAllowed = money(openingNbv.subtract(salvage));
        // Floor: never book below salvage, never negative.
        if (charge.compareTo(maxAllowed) > 0) {
            charge = maxAllowed;
        }
        if (charge.signum() < 0) {
            charge = BigDecimal.ZERO.setScale(MONEY_SCALE, ROUNDING);
        }
        return charge;
    }
}
