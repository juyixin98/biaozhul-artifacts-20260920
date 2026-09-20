package com.itasset.service;

import com.itasset.domain.DepreciationMethod;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 月度折旧计算（纯函数，无副作用，全部定点数）。
 *
 * <h3>舍入规则</h3>
 * <ul>
 *   <li>金额存储定点 4 位小数（DECIMAL(18,4)）；</li>
 *   <li>月度计提额按人民币分位 {@code HALF_UP}（四舍五入）保留 2 位小数；</li>
 *   <li>每个折旧段的最后一个月采用补差法：计提额 = 期初账面价值 - 残值，
 *       期末恰好等于残值，不依赖逐月除法的舍入累积；</li>
 *   <li>任何方法下期末账面价值都不得低于残值：触达残值后计提额为 0。</li>
 * </ul>
 *
 * <h3>折旧段</h3>
 * 初始段覆盖启用次月起的全部预计使用月；参数调整（未来适用法）开启新段：
 * 以调整时点账面价值为新基数，在剩余使用月内摊销。当月增加次月起计提；
 * 退役/处置当月仍计提，次月起停止（由服务层按状态变更月份门控）。
 */
public final class DepreciationCalculator {

    /** 月度入账金额保留 2 位小数（分）。 */
    public static final int POSTING_SCALE = 2;
    /** 账面金额保留 4 位小数。 */
    public static final int BOOK_SCALE = 4;
    private static final BigDecimal HUNDRED = new BigDecimal("100");

    private DepreciationCalculator() {
    }

    /**
     * @param method             折旧方法
     * @param openingBookValue   期初账面价值
     * @param salvageValue       当前残值
     * @param depreciableBase    本段可折旧基数（期初 - 残值）；余额递减法不使用
     * @param segmentMonthIndex  本段内月序号（1 基）
     * @param segmentMonths      本段总月数（初始=使用月数；调整后=剩余使用月数）
     * @param annualRatePct      余额递减法年折旧率百分数（如 40.0000）；直线法传 null
     */
    public record Input(
            DepreciationMethod method,
            BigDecimal openingBookValue,
            BigDecimal salvageValue,
            BigDecimal depreciableBase,
            int segmentMonthIndex,
            int segmentMonths,
            BigDecimal annualRatePct) {
    }

    public record Result(
            BigDecimal openingBookValue,
            BigDecimal depreciationAmount,
            BigDecimal closingBookValue,
            BigDecimal monthlyRatePct,
            boolean segmentFinalMonth) {
    }

    public static Result calculate(Input in) {
        BigDecimal opening = in.openingBookValue().setScale(BOOK_SCALE, RoundingMode.HALF_UP);
        BigDecimal salvage = in.salvageValue().setScale(BOOK_SCALE, RoundingMode.HALF_UP);
        BigDecimal headroom = opening.subtract(salvage);
        boolean lastMonth = in.segmentMonthIndex() >= in.segmentMonths();

        // 已触达残值：计提为 0。
        if (headroom.compareTo(BigDecimal.ZERO) <= 0) {
            return new Result(opening, BigDecimal.ZERO.setScale(POSTING_SCALE),
                    opening, monthlyRate(in), false);
        }

        BigDecimal amount;
        if (lastMonth) {
            // 末月补差：期末精确等于残值。
            amount = money(headroom);
        } else if (in.method() == DepreciationMethod.STRAIGHT_LINE) {
            BigDecimal monthlyRaw = in.depreciableBase()
                    .divide(BigDecimal.valueOf(in.segmentMonths()), 10, RoundingMode.HALF_UP);
            amount = money(monthlyRaw);
        } else {
            BigDecimal monthlyRate = in.annualRatePct()
                    .divide(HUNDRED.multiply(BigDecimal.valueOf(12)), 10, RoundingMode.HALF_UP);
            amount = money(opening.multiply(monthlyRate));
        }

        // 残值地板 + 非负保护（中间月份也可能因舍入触及残值）。
        if (amount.compareTo(headroom) > 0) {
            amount = money(headroom);
        }
        if (amount.signum() < 0) {
            amount = BigDecimal.ZERO.setScale(POSTING_SCALE);
        }
        return new Result(opening, amount,
                opening.subtract(amount).setScale(BOOK_SCALE, RoundingMode.HALF_UP),
                monthlyRate(in), lastMonth && amount.signum() > 0);
    }

    /** 展示用月折旧率百分数：余额递减 = 年率/12；直线法返回 null（导出层以"基数/月数"解释）。 */
    private static BigDecimal monthlyRate(Input in) {
        if (in.method() == DepreciationMethod.DECLINING_BALANCE && in.annualRatePct() != null) {
            return in.annualRatePct().divide(BigDecimal.valueOf(12), 6, RoundingMode.HALF_UP);
        }
        return null;
    }

    private static BigDecimal money(BigDecimal v) {
        return v.setScale(POSTING_SCALE, RoundingMode.HALF_UP);
    }
}
