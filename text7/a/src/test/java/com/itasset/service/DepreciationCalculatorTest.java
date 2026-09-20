package com.itasset.service;

import com.itasset.domain.DepreciationMethod;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;

import static org.assertj.core.api.Assertions.assertThat;

/** 折旧计算器纯单元测试：舍入规则与残值边界。 */
class DepreciationCalculatorTest {

    private DepreciationCalculator.Input sl(int idx, int months, BigDecimal opening,
                                            BigDecimal base, BigDecimal salvage) {
        return new DepreciationCalculator.Input(DepreciationMethod.STRAIGHT_LINE,
                opening, salvage, base, idx, months, null);
    }

    @Test
    void straightLine_roundsMonthlyToCent() {
        // 可折旧基数 995 / 3 = 331.6666... -> 331.67
        var r = DepreciationCalculator.calculate(
                sl(1, 3, new BigDecimal("1000.0000"), new BigDecimal("995.0000"),
                        new BigDecimal("5.0000")));
        assertThat(r.depreciationAmount()).isEqualByComparingTo("331.67");
        assertThat(r.closingBookValue()).isEqualByComparingTo("668.3300");
        assertThat(r.segmentFinalMonth()).isFalse();

        var r2 = DepreciationCalculator.calculate(
                sl(2, 3, new BigDecimal("668.3300"), new BigDecimal("995.0000"),
                        new BigDecimal("5.0000")));
        assertThat(r2.depreciationAmount()).isEqualByComparingTo("331.67");
    }

    @Test
    void straightLine_finalMonthPlugsToSalvageExactly() {
        // 前两期各 331.67（共 663.34），末月补差 331.66，期末恰好 = 残值 5
        var r3 = DepreciationCalculator.calculate(
                sl(3, 3, new BigDecimal("336.6600"), new BigDecimal("995.0000"),
                        new BigDecimal("5.0000")));
        assertThat(r3.segmentFinalMonth()).isTrue();
        assertThat(r3.depreciationAmount()).isEqualByComparingTo("331.66");
        assertThat(r3.closingBookValue()).isEqualByComparingTo("5.0000");
    }

    @Test
    void straightLine_evenDivision() {
        // 12000 / 12 = 1000
        var r = DepreciationCalculator.calculate(
                sl(1, 12, new BigDecimal("12000.0000"), new BigDecimal("12000.0000"),
                        BigDecimal.ZERO.setScale(4)));
        assertThat(r.depreciationAmount()).isEqualByComparingTo("1000.00");
        assertThat(r.closingBookValue()).isEqualByComparingTo("11000.0000");
    }

    @Test
    void bookValueNeverBelowSalvage_whenRoundingWouldCross() {
        // 残值 10，期初 10.005（4 位精度），非末月直线月额 0.01 > headroom 0.005
        // -> 压到残值地板
        var r = DepreciationCalculator.calculate(
                sl(1, 2, new BigDecimal("10.0050"), new BigDecimal("0.0100"),
                        new BigDecimal("10.0000")));
        assertThat(r.depreciationAmount()).isEqualByComparingTo("0.01");
        assertThat(r.closingBookValue()).isEqualByComparingTo("9.9950"); // opening 10.005 - 0.01

        // 期初已等于残值 -> 计提 0
        var zero = DepreciationCalculator.calculate(
                sl(1, 2, new BigDecimal("10.0000"), new BigDecimal("0"),
                        new BigDecimal("10.0000")));
        assertThat(zero.depreciationAmount()).isEqualByComparingTo("0.00");
        assertThat(zero.closingBookValue()).isEqualByComparingTo("10.0000");
    }

    @Test
    void decliningBalance_monthlyRateAndSalvagePlug() {
        // 年率 40% -> 月率 3.333333%；6000 * 3.333333% = 200.00
        var in1 = new DepreciationCalculator.Input(DepreciationMethod.DECLINING_BALANCE,
                new BigDecimal("6000.0000"), new BigDecimal("500.0000"), null,
                1, 24, new BigDecimal("40.0000"));
        var r1 = DepreciationCalculator.calculate(in1);
        assertThat(r1.depreciationAmount()).isEqualByComparingTo("200.00");
        assertThat(r1.closingBookValue()).isEqualByComparingTo("5800.0000");
        assertThat(r1.monthlyRatePct()).isEqualByComparingTo("3.333333");

        // 5800 * 3.333333% = 193.3333 -> 193.33
        var in2 = new DepreciationCalculator.Input(DepreciationMethod.DECLINING_BALANCE,
                new BigDecimal("5800.0000"), new BigDecimal("500.0000"), null,
                2, 24, new BigDecimal("40.0000"));
        var r2 = DepreciationCalculator.calculate(in2);
        assertThat(r2.depreciationAmount()).isEqualByComparingTo("193.33");
        assertThat(r2.closingBookValue()).isEqualByComparingTo("5606.6700");

        // 末月补差到残值
        var last = DepreciationCalculator.calculate(
                new DepreciationCalculator.Input(DepreciationMethod.DECLINING_BALANCE,
                        new BigDecimal("521.3700"), new BigDecimal("500.0000"), null,
                        24, 24, new BigDecimal("40.0000")));
        assertThat(last.depreciationAmount()).isEqualByComparingTo("21.37");
        assertThat(last.closingBookValue()).isEqualByComparingTo("500.0000");
    }
}
