package com.example.asset.service;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 折旧计算器。统一规则：
 * <ul>
 *   <li>金额保留 2 位小数，HALF_UP（四舍五入）舍入；</li>
 *   <li>按月计提，当月启用、次月计提；</li>
 *   <li>账面价值不得低于残值（每期计提额被 clamp 到 opening - salvage）；</li>
 *   <li>最后一个剩余月份计提全部剩余应折旧额，保证期末恰好等于残值，消除累计舍入误差。</li>
 * </ul>
 */
public final class DepreciationCalculator {

    private static final int SCALE = 2;
    private static final BigDecimal ZERO = BigDecimal.ZERO.setScale(SCALE, RoundingMode.HALF_UP);

    private DepreciationCalculator() {
    }

    /**
     * 直线法（未来适用）：每期计提 = round((期初账面 - 残值) / 剩余月数)。
     * 参数调整后剩余应折旧额在剩余月份内重新平均，已关账期间不受影响。
     */
    public static BigDecimal straightLine(BigDecimal opening, BigDecimal salvage, int remainingMonths) {
        if (remainingMonths <= 0) {
            return ZERO;
        }
        BigDecimal depreciable = opening.subtract(salvage);
        if (depreciable.signum() <= 0) {
            return ZERO;
        }
        if (remainingMonths == 1) {
            return depreciable.setScale(SCALE, RoundingMode.HALF_UP);
        }
        BigDecimal monthly = depreciable.divide(BigDecimal.valueOf(remainingMonths), SCALE, RoundingMode.HALF_UP);
        return monthly.min(depreciable);
    }

    /**
     * 双倍余额递减法：月折旧率 = 2 / 使用年限（月），每期计提 = round(期初账面 × 月折旧率)。
     * 不扣残值计算，但账面价值不得低于残值；剩余最后两个月改按直线法摊销，
     * 保证到期账面价值恰好等于残值。
     */
    public static BigDecimal decliningBalance(BigDecimal opening, BigDecimal salvage,
                                              int remainingMonths, int usefulLifeMonths) {
        if (remainingMonths <= 0 || usefulLifeMonths <= 0) {
            return ZERO;
        }
        if (remainingMonths <= 2) {
            return straightLine(opening, salvage, remainingMonths);
        }
        BigDecimal depreciable = opening.subtract(salvage);
        if (depreciable.signum() <= 0) {
            return ZERO;
        }
        BigDecimal monthlyRate = BigDecimal.valueOf(2.0 / usefulLifeMonths);
        BigDecimal amount = opening.multiply(monthlyRate).setScale(SCALE, RoundingMode.HALF_UP);
        return amount.min(depreciable);
    }
}
