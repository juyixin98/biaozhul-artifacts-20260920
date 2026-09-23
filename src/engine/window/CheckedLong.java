package engine.window;

/**
 * 可检查溢出的整数运算。
 *
 * <p>聚合值一律使用 64 位有符号整数（{@code long}）。每一步累加都通过
 * {@link #addExact(long, long)} 完成：结果溢出 [Long.MIN_VALUE,
 * Long.MAX_VALUE] 时抛出 {@link ArithmeticException}，绝不静默回绕。
 *
 * <p>说明：滑动窗口求和的生产实现按帧用 {@code BigInteger} 前缀和求值，
 * 再用 {@code longValueExact()} 对“整窗数学和”做一次性范围检查
 * （见 {@code WindowEngine}）。本类保留逐操作版本，用于单测、参考实现，
 * 以及在小规模数据上与前缀和路径交叉验证。
 */
public final class CheckedLong {

    private CheckedLong() {
    }

    /** 与 {@link Math#addExact(long, long)} 一致，语义独立、异常信息更明确。 */
    public static long addExact(long a, long b) {
        long r = a + b;
        // 同号相加结果变号，即溢出
        if (((a ^ r) & (b ^ r)) < 0) {
            throw new ArithmeticException("整数溢出: " + a + " + " + b);
        }
        return r;
    }
}
