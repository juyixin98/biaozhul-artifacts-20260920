package com.winquant;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 分位数定义（线性插值）与参考实现的单元测试。 */
class QuantilesTest {

    @Test
    void emptyIsUndefined() {
        assertTrue(Double.isNaN(Quantiles.referenceQuantile(new long[0], 0.5)));
    }

    @Test
    void singleValue() {
        assertEquals(42.0, Quantiles.referenceQuantile(new long[]{42}, 0.5));
        assertEquals(-7.0, Quantiles.referenceQuantile(new long[]{-7}, 0.0));
        assertEquals(-7.0, Quantiles.referenceQuantile(new long[]{-7}, 1.0));
    }

    @Test
    void medianOddCount() {
        assertEquals(2.0, Quantiles.referenceQuantile(new long[]{3, 1, 2}, 0.5));
    }

    @Test
    void medianEvenCountInterpolates() {
        // [1,2,3,4] 中位数 = (2+3)/2 = 2.5
        assertEquals(2.5, Quantiles.referenceQuantile(new long[]{4, 2, 1, 3}, 0.5));
    }

    @Test
    void quartilesUseLinearInterpolation() {
        // [1,2,3,4]: h = q*(n-1)
        // q=0.25 → h=0.75 → 1 + 0.75*(2-1) = 1.75
        // q=0.75 → h=2.25 → 3 + 0.25*(4-3) = 3.25
        long[] v = {1, 2, 3, 4};
        assertEquals(1.0, Quantiles.referenceQuantile(v, 0.0));
        assertEquals(1.75, Quantiles.referenceQuantile(v, 0.25));
        assertEquals(3.25, Quantiles.referenceQuantile(v, 0.75));
        assertEquals(4.0, Quantiles.referenceQuantile(v, 1.0));
    }

    @Test
    void duplicatesOccupyRanks() {
        // [5,5,5] 的所有分位数都是 5
        long[] v = {5, 5, 5};
        assertEquals(5.0, Quantiles.referenceQuantile(v, 0.5));
        assertEquals(5.0, Quantiles.referenceQuantile(v, 0.1));
        // [1,1,3]: median = 1；q=0.9 → h=1.8 → 1 + 0.8*(3-1) = 2.6
        assertEquals(1.0, Quantiles.referenceQuantile(new long[]{1, 1, 3}, 0.5));
        assertEquals(2.6, Quantiles.referenceQuantile(new long[]{1, 1, 3}, 0.9), 1e-12);
    }

    @Test
    void negativeValues() {
        // [-10,-3,0,7] median = (-3+0)/2 = -1.5
        assertEquals(-1.5, Quantiles.referenceQuantile(new long[]{7, -10, 0, -3}, 0.5));
        assertEquals(-10.0, Quantiles.referenceQuantile(new long[]{7, -10, 0, -3}, 0.0));
    }

    @Test
    void extremeLongsDoNotOverflow() {
        long[] v = {Long.MIN_VALUE, Long.MAX_VALUE};
        // 插值在 double 域进行，long 差值不溢出
        assertEquals(-0.5, Quantiles.referenceQuantile(v, 0.5), 1.0);
    }

    @Test
    void rejectsBadQ() {
        assertThrows(IllegalArgumentException.class, () -> Quantiles.referenceQuantile(new long[]{1}, -0.1));
        assertThrows(IllegalArgumentException.class, () -> Quantiles.referenceQuantile(new long[]{1}, 1.1));
        assertThrows(IllegalArgumentException.class, () -> Quantiles.referenceQuantile(new long[]{1}, Double.NaN));
    }
}
