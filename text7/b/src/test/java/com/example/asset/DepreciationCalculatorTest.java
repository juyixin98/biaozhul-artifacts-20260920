package com.example.asset;

import com.example.asset.service.DepreciationCalculator;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;

import static org.assertj.core.api.Assertions.assertThat;

/** 折旧计算器纯单元测试：舍入规则与残值边界。 */
class DepreciationCalculatorTest {

    @Test
    void straightLineRoundsHalfUpAndLastMonthTakesRemainder() {
        // 100.00 / 3 = 33.333... -> 前两月 33.33，最后一月取剩余 33.34
        BigDecimal m1 = DepreciationCalculator.straightLine(bd("100.00"), bd("0.00"), 3);
        assertThat(m1).isEqualByComparingTo("33.33");
        BigDecimal m2 = DepreciationCalculator.straightLine(bd("66.67"), bd("0.00"), 2);
        assertThat(m2).isEqualByComparingTo("33.34"); // 66.67/2 = 33.335 -> HALF_UP 33.34
        BigDecimal m3 = DepreciationCalculator.straightLine(bd("33.33"), bd("0.00"), 1);
        assertThat(m3).isEqualByComparingTo("33.33"); // 最后一月取全部剩余
    }

    @Test
    void straightLineNeverBelowSalvage() {
        // 账面 10.00，残值 8.00，剩余 5 月：每月最多 2.00，不会击穿残值
        BigDecimal amount = DepreciationCalculator.straightLine(bd("10.00"), bd("8.00"), 5);
        assertThat(amount).isEqualByComparingTo("0.40");
        // 账面已等于残值：计提 0
        assertThat(DepreciationCalculator.straightLine(bd("8.00"), bd("8.00"), 5))
                .isEqualByComparingTo("0.00");
    }

    @Test
    void decliningBalanceClampsAtSalvageAndEndsExactlyAtSalvage() {
        BigDecimal opening = bd("10000.00");
        BigDecimal salvage = bd("500.00");
        int life = 12;
        for (int elapsed = 0; elapsed < life; elapsed++) {
            int remaining = life - elapsed;
            BigDecimal amount = DepreciationCalculator.decliningBalance(opening, salvage, remaining, life);
            BigDecimal closing = opening.subtract(amount);
            assertThat(closing).as("month %s must not go below salvage", elapsed + 1)
                    .isGreaterThanOrEqualTo(salvage);
            opening = closing;
        }
        // 最后两个月改直线摊销，到期恰好等于残值
        assertThat(opening).isEqualByComparingTo("500.00");
    }

    @Test
    void decliningBalanceZeroWhenFullyDepreciatedToSalvage() {
        assertThat(DepreciationCalculator.decliningBalance(bd("500.00"), bd("500.00"), 10, 60))
                .isEqualByComparingTo("0.00");
    }

    private static BigDecimal bd(String v) {
        return new BigDecimal(v);
    }
}
