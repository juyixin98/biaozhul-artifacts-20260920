package com.example.itasset.depreciation;

import com.example.itasset.domain.DepreciationMethod;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 折旧计算口径纯单元测试（无数据库）：月度计提、HALF_UP 舍入、残值下限、末期补差。
 */
class DepreciationCalculatorTest {

    private static BigDecimal bd(String v) {
        return new BigDecimal(v).setScale(2);
    }

    @Test
    void straightLine_constantMonthlyCharge_halfUp() {
        // 12000 - 600 = 11400 / 36 = 316.666... -> 316.67（四舍五入）
        var line = DepreciationCalculator.compute(DepreciationMethod.STRAIGHT_LINE,
                bd("12000.00"), bd("600.00"), 202601, 1, 36, 36);
        assertThat(line.charge()).isEqualByComparingTo("316.67");
        assertThat(line.closing()).isEqualByComparingTo("11683.33");

        // 第二个月基数为剩余 35 个月：(11683.33-600)/35 = 316.6666 -> 316.67
        var line2 = DepreciationCalculator.compute(DepreciationMethod.STRAIGHT_LINE,
                bd("11683.33"), bd("600.00"), 202602, 2, 36, 36);
        assertThat(line2.charge()).isEqualByComparingTo("316.67");
    }

    @Test
    void straightLine_finalMonthPlugsExactlyToSalvage() {
        var line = DepreciationCalculator.compute(DepreciationMethod.STRAIGHT_LINE,
                bd("610.00"), bd("600.00"), 202812, 36, 36, 36);
        assertThat(line.charge()).isEqualByComparingTo("10.00");
        assertThat(line.closing()).isEqualByComparingTo("600.00");
    }

    @Test
    void decliningBalance_doubleRateWithSalvageFloor() {
        // 月率 = 2/48 ≈ 0.041667（6 位小数）；60000 × 0.041667 = 2500.02
        var line = DepreciationCalculator.compute(DepreciationMethod.DECLINING_BALANCE,
                bd("60000.00"), bd("3000.00"), 202603, 1, 48, 48);
        assertThat(line.charge()).isEqualByComparingTo("2500.02");
        assertThat(line.closing()).isEqualByComparingTo("57499.98");
    }

    @Test
    void decliningBalance_lastMonthPlugsToSalvageNeverBelow() {
        // 末期无论余额多少都补差到残值
        var line = DepreciationCalculator.compute(DepreciationMethod.DECLINING_BALANCE,
                bd("3800.00"), bd("3000.00"), 203002, 48, 48, 48);
        assertThat(line.charge()).isEqualByComparingTo("800.00");
        assertThat(line.closing()).isEqualByComparingTo("3000.00");
    }

    @Test
    void chargeCappedSoClosingNeverDropsBelowSalvage() {
        // 非末期但按率计算后会跌破残值 -> 计提被夹到 期初-残值
        var line = DepreciationCalculator.compute(DepreciationMethod.DECLINING_BALANCE,
                bd("3050.00"), bd("3000.00"), 203001, 47, 48, 48);
        assertThat(line.charge()).isEqualByComparingTo("50.00");
        assertThat(line.closing()).isEqualByComparingTo("3000.00");
    }

    @Test
    void zeroChargeWhenAlreadyAtSalvage() {
        var line = DepreciationCalculator.compute(DepreciationMethod.STRAIGHT_LINE,
                bd("600.00"), bd("600.00"), 202811, 35, 36, 36);
        assertThat(line.charge()).isEqualByComparingTo("0.00");
        assertThat(line.closing()).isEqualByComparingTo("600.00");
    }

    @Test
    void rejectsIndexOutsideWindow() {
        assertThatThrownBy(() -> DepreciationCalculator.compute(
                DepreciationMethod.STRAIGHT_LINE, bd("12000"), bd("600"),
                202901, 37, 36, 36))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
