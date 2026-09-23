package com.example.quantiles.quantile;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 基于 {@link TreeMap} 的精确多重集合实现。
 *
 * <ul>
 *   <li>插入/删除：O(log U)，U 为窗口内不同取值的个数；每个取值维护精确多重度，
 *       删除归零立即移除键，<b>过期删除后计数始终正确</b>。</li>
 *   <li>分位数查询：按取值升序扫描定位第 k 个次序统计量，R-7 结果以精确分数给出。</li>
 *   <li>合并：逐取值 O(log U) 插入。</li>
 * </ul>
 * 这是精确实现（不是概率草图）：所有元素都以精确多重度保留，
 * 结果与“全排序后按下标取值并插值”逐位一致。
 */
public final class TreeMapAccumulator implements QuantileAccumulator {

    /** value -> multiplicity，按键（值）升序。 */
    private final TreeMap<Long, Long> counts = new TreeMap<>();
    private long totalCount;

    @Override
    public void add(long value) {
        counts.merge(value, 1L, Long::sum);
        totalCount++;
    }

    @Override
    public void remove(long value) {
        Long c = counts.get(value);
        if (c == null) {
            throw new IllegalStateException("无法删除不存在的值: " + value);
        }
        if (c == 1L) {
            counts.remove(value);
        } else {
            counts.put(value, c - 1);
        }
        totalCount--;
    }

    @Override
    public long count() {
        return totalCount;
    }

    @Override
    public Fraction quantile(double q) {
        if (Double.isNaN(q) || q < 0.0 || q > 1.0) {
            throw new IllegalArgumentException("q 必须在 [0,1] 内: " + q);
        }
        if (totalCount == 0) {
            throw new IllegalStateException("窗口为空，无分位数");
        }
        long n = totalCount;
        // R-7 连续位置 p = q*(n-1)。常用 q（0.5、0.25、0.1 等）按十进制精确转分数，
        // 最终插值结果以精确分数给出并约分（整数输入下中位数等结果不带二进制浮点尾差）。
        long[] qf = rationalize(q); // {num, den}
        BigInteger pNum = BigInteger.valueOf(qf[0]).multiply(BigInteger.valueOf(n - 1));
        BigInteger pDen = BigInteger.valueOf(qf[1]);
        BigInteger[] div = pNum.divideAndRemainder(pDen);
        long loIndex = div[0].longValueExact();
        BigInteger fNumBI = div[1];
        long loValue = kthOrderStatistic(loIndex);
        if (fNumBI.signum() == 0) {
            return Fraction.of(loValue);
        }
        long hiValue = kthOrderStatistic(loIndex + 1);
        if (loValue == hiValue) {
            return Fraction.of(loValue); // 重复值：相邻次序位相同，插值不改变结果
        }
        // result = loValue + (fNum/pDen)*(hiValue-loValue)
        //        = (loValue*pDen + fNum*(hiValue-loValue)) / pDen
        BigInteger num = BigInteger.valueOf(loValue).multiply(pDen)
                .add(fNumBI.multiply(BigInteger.valueOf(hiValue - loValue)));
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

    /**
     * 返回升序排序后第 k 个元素（0 基，重复值按多重度占位）。
     * 单次 O(U) 扫描；调用方保证 0 &lt;= k &lt; n。
     */
    private long kthOrderStatistic(long k) {
        long seen = 0;
        for (Map.Entry<Long, Long> e : counts.entrySet()) {
            seen += e.getValue();
            if (k < seen) {
                return e.getKey();
            }
        }
        throw new AssertionError("kth 越界: k=" + k + " n=" + totalCount);
    }

    /**
     * 把 double q 转成最简正分数（含 0/1、1/1）。
     * 用 {@link java.math.BigDecimal} 基于 double 的十进制字符串精确表示，
     * 因此 0.5 -> 1/2、0.25 -> 1/4、0.1 -> 1/10，避免二进制浮点尾差。
     */
    private static long[] rationalize(double q) {
        if (q == 0.0) {
            return new long[] {0L, 1L};
        }
        if (q == 1.0) {
            return new long[] {1L, 1L};
        }
        BigDecimal stripped = new BigDecimal(Double.toString(q)).stripTrailingZeros();
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

    @Override
    public void addAll(QuantileAccumulator other) {
        if (!(other instanceof TreeMapAccumulator o)) {
            throw new IllegalArgumentException("只支持合并 TreeMapAccumulator: " + other.getClass());
        }
        for (Map.Entry<Long, Long> e : o.counts.entrySet()) {
            counts.merge(e.getKey(), e.getValue(), Long::sum);
        }
        totalCount += o.totalCount;
    }
}
