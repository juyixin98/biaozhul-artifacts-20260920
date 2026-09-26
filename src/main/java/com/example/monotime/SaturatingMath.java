package com.example.monotime;

import java.time.Duration;

/** 纳秒刻度上的饱和算术，防止超长时长在加/减时静默溢出成负数。 */
public final class SaturatingMath {

    private SaturatingMath() {
    }

    public static long addClamped(long a, long b) {
        try {
            return Math.addExact(a, b);
        } catch (ArithmeticException e) {
            return b > 0 ? Long.MAX_VALUE : Long.MIN_VALUE;
        }
    }

    public static long subtractClamped(long a, long b) {
        try {
            return Math.subtractExact(a, b);
        } catch (ArithmeticException e) {
            return b > 0 ? Long.MIN_VALUE : Long.MAX_VALUE;
        }
    }

    /** {@link Duration#toNanos()} 的饱和版本：超出 long 纳秒范围（约 292 年）时钳制。 */
    public static long toNanosSaturated(Duration duration) {
        try {
            return duration.toNanos();
        } catch (ArithmeticException e) {
            return duration.isNegative() ? Long.MIN_VALUE : Long.MAX_VALUE;
        }
    }
}
