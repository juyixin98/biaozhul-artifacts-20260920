package com.example.seg.model;

import java.math.BigDecimal;

/**
 * 代价的统一表示：内部一律用"放大 1e6 倍后的 long 整数"做加减与比较，
 * 这样最小代价路径的比较是精确的整数比较，不依赖 double 的浮点误差；
 * 对外展示时再还原成最多 6 位小数的 BigDecimal。
 */
public final class Costs {

    /** 小数位放大倍数。词典代价最多允许 6 位小数。 */
    public static final long SCALE = 1_000_000L;

    private Costs() {
    }

    /** 把词典里的十进制代价字符串解析成放大后的整数代价。 */
    public static long parseScaled(String text) {
        BigDecimal v = new BigDecimal(text.trim());
        if (v.signum() < 0) {
            throw new IllegalArgumentException("代价不允许为负数: " + text);
        }
        return v.movePointRight(6).longValueExact();
    }

    /** 整数（如词典版本里的未知字符代价）转放大整数。 */
    public static long ofInt(int value) {
        if (value < 0) {
            throw new IllegalArgumentException("代价不允许为负数: " + value);
        }
        return value * SCALE;
    }

    /** 放大整数还原成展示用的 BigDecimal。 */
    public static BigDecimal toBigDecimal(long scaled) {
        return BigDecimal.valueOf(scaled, 6).stripTrailingZeros();
    }

    /** 放大整数还原成展示用的字符串（去掉多余的 0，0 显示为 "0"）。 */
    public static String toString(long scaled) {
        if (scaled == 0L) {
            return "0";
        }
        return toBigDecimal(scaled).toPlainString();
    }
}
