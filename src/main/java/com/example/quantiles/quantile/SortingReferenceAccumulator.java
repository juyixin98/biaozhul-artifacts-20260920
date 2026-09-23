package com.example.quantiles.quantile;

import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 小数据<em>精确参考实现</em>：把窗口内全部元素保留为一个可重复排序的列表，
 * 查询时整体排序后按 R-7 定义直接取下标并插值。
 *
 * <p>该实现朴素（插入/删除为 O(n)），用途是：
 * <ol>
 *   <li>作为验收基准——需求要求“与每窗全排序比较”，本类就是那个全排序；</li>
 *   <li>在随机差分测试中与 {@link TreeMapAccumulator} 逐一对照。</li>
 * </ol>
 */
public final class SortingReferenceAccumulator implements QuantileAccumulator {

    /** 保留每个事件的一份副本（不去重），即窗口的完整多重集。 */
    private final List<Long> values = new ArrayList<>();

    @Override
    public void add(long value) {
        values.add(value);
    }

    @Override
    public void remove(long value) {
        // 删除一个等于 value 的元素（与 TreeMapAccumulator 的多重度删除语义一致）
        int idx = values.indexOf(value);
        if (idx < 0) {
            throw new IllegalStateException("无法删除不存在的值: " + value);
        }
        values.remove(idx);
    }

    @Override
    public long count() {
        return values.size();
    }

    @Override
    public Fraction quantile(double q) {
        if (Double.isNaN(q) || q < 0.0 || q > 1.0) {
            throw new IllegalArgumentException("q 必须在 [0,1] 内: " + q);
        }
        int n = values.size();
        if (n == 0) {
            throw new IllegalStateException("窗口为空，无分位数");
        }
        List<Long> sorted = new ArrayList<>(values);
        Collections.sort(sorted);
        // p = q*(n-1)，与 TreeMapAccumulator 相同的十进制精确分数路径
        long[] qf = rationalize(q);
        BigInteger pNum = BigInteger.valueOf(qf[0]).multiply(BigInteger.valueOf(n - 1L));
        BigInteger pDen = BigInteger.valueOf(qf[1]);
        BigInteger[] div = pNum.divideAndRemainder(pDen);
        int loIndex = div[0].intValueExact();
        BigInteger fNum = div[1];
        long lo = sorted.get(loIndex);
        if (fNum.signum() == 0) {
            return Fraction.of(lo);
        }
        long hi = sorted.get(loIndex + 1);
        if (lo == hi) {
            return Fraction.of(lo);
        }
        BigInteger num = BigInteger.valueOf(lo).multiply(pDen)
                .add(fNum.multiply(BigInteger.valueOf(hi - lo)));
        return new Fraction(num, pDen.longValueExact());
    }

    @Override
    public List<Fraction> quantiles(List<Double> qs) {
        List<Fraction> out = new ArrayList<>(qs.size());
        for (double q : qs) {
            out.add(quantile(q));
        }
        return out;
    }

    @Override
    public void addAll(QuantileAccumulator other) {
        if (!(other instanceof SortingReferenceAccumulator o)) {
            throw new IllegalArgumentException(
                    "只支持合并 SortingReferenceAccumulator: " + other.getClass());
        }
        values.addAll(o.values);
    }

    private static long[] rationalize(double q) {
        if (q == 0.0) {
            return new long[] {0L, 1L};
        }
        if (q == 1.0) {
            return new long[] {1L, 1L};
        }
        java.math.BigDecimal stripped =
                new java.math.BigDecimal(Double.toString(q)).stripTrailingZeros();
        long num = stripped.unscaledValue().longValueExact();
        long den = (long) Math.pow(10, stripped.scale());
        long g = gcd(num, den);
        return new long[] {num / g, den / g};
    }

    private static long gcd(long a, long b) {
        a = Math.abs(a);
        while (b != 0) {
            long t = a % b;
            a = b;
            b = t;
        }
        return a == 0 ? 1 : a;
    }
}
