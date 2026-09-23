package com.example.quantiles.quantile;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.math.MathContext;

/**
 * 精确分数。事件值为整数，R-7 插值只产生 {@code v} 或 {@code (v1+v2)/2}，
 * 分母恒为 1 或 2。分子用 {@link BigInteger} 存储，避免两个 long 相加溢出。
 * 构造时约分并令分母为正。
 */
public record Fraction(BigInteger numerator, long denominator) {

    public Fraction {
        if (denominator <= 0) {
            throw new IllegalArgumentException("denominator 必须为正: " + denominator);
        }
        if (numerator.signum() == 0) {
            denominator = 1;
        } else {
            BigInteger g = numerator.abs().gcd(BigInteger.valueOf(denominator));
            if (g.compareTo(BigInteger.ONE) > 0) {
                numerator = numerator.divide(g);
                denominator = BigInteger.valueOf(denominator).divide(g).longValueExact();
            }
        }
    }

    /** 整数值。 */
    public static Fraction of(long value) {
        return new Fraction(BigInteger.valueOf(value), 1);
    }

    /** 两整数的精确平均值（和为奇数时得到 .5），中间和不溢出。 */
    public static Fraction average(long a, long b) {
        BigInteger sum = BigInteger.valueOf(a).add(BigInteger.valueOf(b));
        return new Fraction(sum, 2);
    }

    /** 转 double（本类分母只可能为 1 或 2，转换精确）。 */
    public double doubleValue() {
        return new BigDecimal(numerator)
                .divide(BigDecimal.valueOf(denominator), MathContext.DECIMAL128)
                .doubleValue();
    }

    /**
     * 规范数字文本：整数不带小数点，半整数带 ".5"，负号在最前。
     * JSON 输出使用该形式，避免出现 "5.0"。
     */
    public String toCanonicalString() {
        if (denominator == 1) {
            return numerator.toString();
        }
        return new BigDecimal(numerator)
                .divide(BigDecimal.valueOf(denominator), MathContext.DECIMAL128)
                .stripTrailingZeros()
                .toPlainString();
    }

    @Override
    public String toString() {
        return denominator == 1 ? numerator.toString() : numerator + "/" + denominator;
    }
}
