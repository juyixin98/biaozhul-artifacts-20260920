package com.winquant;

import java.util.Arrays;

/**
 * 精确分位数计算（非近似草图）。
 *
 * <h2>分位数定义（线性插值，等同 numpy 默认 "linear" / R type-7）</h2>
 * 设窗口内共有 n 个值，排序后为 x[0] &le; x[1] &le; ... &le; x[n-1]，q &isin; [0,1]：
 * <ul>
 *   <li>n = 0：分位数无定义，返回 {@code null}（Java 侧用 {@link java.util.OptionalDouble} 空值表示）；</li>
 *   <li>令 h = q * (n - 1)，i = floor(h)，f = h - i；</li>
 *   <li>结果 = x[i] + f * (x[i+1] - x[i])（当 i = n-1 时结果就是 x[n-1]）。</li>
 * </ul>
 *
 * <h2>重复值规则</h2>
 * 每个事件独立计数，重复值按出现次数占用位次。例如窗口 [5,5,5] 的中位数是 5。
 *
 * <h2>中位数</h2>
 * median = quantile(0.5)。n 为偶数时为中间两值的算术平均，例如 [1,2,3,4] → 2.5。
 */
public final class Quantiles {

    private Quantiles() {
    }

    /**
     * 对已排序（升序）数组求精确分位数。
     *
     * @param sortedAscending 升序排列的值，调用方保证已排序
     * @param q               分位点，[0,1]
     * @return 精确分位数值；数组为空时返回 {@link Double#NaN}
     */
    public static double quantileOfSorted(long[] sortedAscending, double q) {
        if (q < 0.0 || q > 1.0 || Double.isNaN(q)) {
            throw new IllegalArgumentException("q must be in [0,1], got " + q);
        }
        int n = sortedAscending.length;
        if (n == 0) {
            return Double.NaN;
        }
        if (n == 1) {
            return sortedAscending[0];
        }
        double h = q * (n - 1);
        int i = (int) Math.floor(h);
        double f = h - i;
        long lo = sortedAscending[i];
        if (f == 0.0 || i + 1 >= n) {
            return lo;
        }
        long hi = sortedAscending[i + 1];
        // 用 double 计算差值，避免 long 溢出影响结果（long 差值可能溢出，转 double 后安全）
        return lo + f * ((double) hi - (double) lo);
    }

    /**
     * 小数据精确参考实现：复制、全排序、按定义取值。
     * 用于测试中与增量滑动窗口实现逐点对比，也可直接服务小窗口。
     */
    public static double referenceQuantile(long[] values, double q) {
        long[] copy = Arrays.copyOf(values, values.length);
        Arrays.sort(copy);
        return quantileOfSorted(copy, q);
    }

    /** 精确中位数，即 quantile(0.5)。 */
    public static double medianOfSorted(long[] sortedAscending) {
        return quantileOfSorted(sortedAscending, 0.5);
    }
}
