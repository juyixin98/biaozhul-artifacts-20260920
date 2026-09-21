package com.example.itasset.depreciation;

import com.example.itasset.domain.DepreciationMethod;
import com.example.itasset.support.Periods;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 单条政策窗口内的月度折旧计算（无状态、可单测）。
 *
 * <p>统一口径：</p>
 * <ul>
 *   <li>金额定点数 DECIMAL(18,2)；中间除法保留 6 位小数，落账金额按
 *       {@link RoundingMode#HALF_UP} 保留 2 位（四舍五入）。</li>
 *   <li>整月口径：启用当月计提一整月。</li>
 *   <li>政策窗口最后一个月采用“补差”口径：charge = 期初净值 − 残值，
 *       保证期末账面净值恰好等于残值；任何月份账面净值不得低于残值。</li>
 * </ul>
 *
 * <p>直线法：月计提 =（生效期初账面价值 − 残值）/ 剩余月数。</p>
 * <p>余额递减法（双倍余额递减，不切换直线法）：月率 = 2 / 资产原始预计使用月数，
 * 月计提 = 期初账面净值 × 月率，受残值下限与末期补差约束。</p>
 */
public final class DepreciationCalculator {

    /** 中间计算精度。 */
    public static final int CALC_SCALE = 6;
    /** 落账金额精度（分）。 */
    public static final int MONEY_SCALE = 2;
    public static final RoundingMode ROUNDING = RoundingMode.HALF_UP;

    private DepreciationCalculator() {
    }

    /**
     * @param opening 该月期初账面净值（已按 2 位小数）
     * @param salvage 当前政策残值
     * @param period 当前计提期间 YYYYMM
     * @param indexInWindow 该月在政策窗口内的序号，从 1 开始
     * @param windowMonths 当前政策剩余可计提月数（窗口长度）
     * @param originalLifeMonths 资产原始预计使用月数（双倍月率分母）
     */
    public static LineResult compute(DepreciationMethod method,
                                     BigDecimal opening,
                                     BigDecimal salvage,
                                     int period,
                                     int indexInWindow,
                                     int windowMonths,
                                     int originalLifeMonths) {
        if (indexInWindow < 1 || indexInWindow > windowMonths) {
            throw new IllegalArgumentException("月份序号超出政策窗口: " + indexInWindow + "/" + windowMonths);
        }
        boolean lastMonth = indexInWindow == windowMonths;
        if (lastMonth) {
            // 末期补差：确保净值精确落在残值
            BigDecimal plug = opening.subtract(salvage).setScale(MONEY_SCALE, ROUNDING);
            if (plug.signum() < 0) {
                plug = BigDecimal.ZERO.setScale(MONEY_SCALE, ROUNDING);
            }
            return new LineResult(opening, plug, opening.subtract(plug),
                    "末期补差至残值: 计提 = 期初净值 " + opening + " − 残值 " + salvage + " = " + plug);
        }

        BigDecimal rawCharge = switch (method) {
            case STRAIGHT_LINE -> {
                BigDecimal base = opening.subtract(salvage);
                BigDecimal remaining = BigDecimal.valueOf(windowMonths - indexInWindow + 1L);
                yield base.divide(remaining, CALC_SCALE, ROUNDING);
            }
            case DECLINING_BALANCE -> {
                BigDecimal rate = BigDecimal.valueOf(2)
                        .divide(BigDecimal.valueOf(originalLifeMonths), CALC_SCALE, ROUNDING);
                yield opening.multiply(rate);
            }
        };

        BigDecimal charge = rawCharge.setScale(MONEY_SCALE, ROUNDING);

        // 残值下限：本期计提不得使期末净值低于残值
        BigDecimal maxCharge = opening.subtract(salvage);
        if (charge.compareTo(maxCharge) > 0) {
            charge = maxCharge.setScale(MONEY_SCALE, ROUNDING);
        }
        if (charge.signum() < 0) {
            charge = BigDecimal.ZERO.setScale(MONEY_SCALE, ROUNDING);
        }
        BigDecimal closing = opening.subtract(charge).setScale(MONEY_SCALE, ROUNDING);

        String detail = switch (method) {
            case STRAIGHT_LINE -> "直线法: 计提 = (期初净值 " + opening + " − 残值 " + salvage
                    + ") / 剩余月数 " + (windowMonths - indexInWindow + 1)
                    + " = " + charge + "（HALF_UP 保留两位）";
            case DECLINING_BALANCE -> "余额递减法: 月率 = 2 / 原始月数 " + originalLifeMonths
                    + " = " + BigDecimal.valueOf(2)
                    .divide(BigDecimal.valueOf(originalLifeMonths), CALC_SCALE, ROUNDING)
                    + "；计提 = 期初净值 " + opening + " × 月率 = " + charge
                    + "（HALF_UP 保留两位，残值下限 " + salvage
                    + "，期间 " + Periods.format(period) + "）";
        };
        return new LineResult(opening, charge, closing, detail);
    }

    /**
     * 一期计算结果。
     */
    public record LineResult(BigDecimal opening, BigDecimal charge, BigDecimal closing, String detail) {
    }
}
