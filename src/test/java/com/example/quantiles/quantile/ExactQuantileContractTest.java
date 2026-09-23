package com.example.quantiles.quantile;

import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * 精确分位数规则的固定用例（R-7，与 numpy/SQL PERCENTILE_CONT 一致）。
 */
class ExactQuantileContractTest {

    private QuantileAccumulator acc(long... values) {
        TreeMapAccumulator a = new TreeMapAccumulator();
        for (long v : values) {
            a.add(v);
        }
        return a;
    }

    @Nested
    class Median {

        @Test
        void oddCountIsMiddleElement() {
            assertEquals("3", acc(1, 2, 3, 4, 5).quantile(0.5).toCanonicalString());
        }

        @Test
        void evenCountIsAverageOfTwoMiddleValues() {
            assertEquals("2.5", acc(1, 2, 3, 4).quantile(0.5).toCanonicalString());
        }

        @Test
        void singleElementIsItself() {
            assertEquals("42", acc(42).quantile(0.5).toCanonicalString());
        }

        @Test
        void twoElementsAreTheirAverage() {
            assertEquals("5.5", acc(1, 10).quantile(0.5).toCanonicalString());
            assertEquals("-5.5", acc(-1, -10).quantile(0.5).toCanonicalString());
        }
    }

    @Nested
    class Endpoints {

        @Test
        void q0IsMinimumAndQ1IsMaximum() {
            var a = acc(-9, 100, 0, 5, -3);
            assertEquals("-9", a.quantile(0.0).toCanonicalString());
            assertEquals("100", a.quantile(1.0).toCanonicalString());
        }
    }

    @Nested
    class Duplicates {

        @Test
        void allDuplicateValuesReturnThatValue() {
            var a = acc(7, 7, 7, 7, 7);
            for (double q : List.of(0.0, 0.25, 0.5, 0.75, 0.99, 1.0)) {
                assertEquals("7", a.quantile(q).toCanonicalString(),
                        "全重复数据在任意分位都应等于该值, q=" + q);
            }
        }

        @Test
        void duplicatesOccupyMultipleOrderSlots() {
            // [1, 2, 2, 2, 5]：连续位置 p=q*4（0基）
            var a = acc(2, 5, 2, 1, 2);
            assertEquals("1", a.quantile(0.0).toCanonicalString());
            assertEquals("2", a.quantile(0.25).toCanonicalString()); // p=1
            assertEquals("2", a.quantile(0.5).toCanonicalString());  // p=2
            assertEquals("2", a.quantile(0.75).toCanonicalString()); // p=3 -> v[3]=2
            assertEquals("3.8", a.quantile(0.9).toCanonicalString()); // p=3.6 -> 2+0.6*(5-2)
            assertEquals("5", a.quantile(1.0).toCanonicalString());
        }

        @Test
        void medianOfEvenLengthDuplicatedMiddleIsInteger() {
            // [1,1,10,10] -> 中间两个都是“重复块” -> (1+10)/2
            assertEquals("5.5", acc(1, 1, 10, 10).quantile(0.5).toCanonicalString());
            // [1,10,10,10] -> (10+10)/2 = 10
            assertEquals("10", acc(1, 10, 10, 10).quantile(0.5).toCanonicalString());
        }
    }

    @Nested
    class NegativeValues {

        @Test
        void medianAcrossNegativesAndZero() {
            assertEquals("-1", acc(-5, -3, -1, 0, 2).quantile(0.5).toCanonicalString());
            assertEquals("-2", acc(-5, -3, -1, 0).quantile(0.5).toCanonicalString());
        }
    }

    @Nested
    class Interpolation {

        @Test
        void r7LinearInterpolationMatchesDefinition() {
            // n=4, sorted [0,10,20,30]: 连续位置 p=3q
            var a = acc(0, 10, 20, 30);
            assertEquals("15", a.quantile(0.5).toCanonicalString());   // p=1.5 -> (10+20)/2
            assertEquals("3", a.quantile(0.1).toCanonicalString());     // p=0.3 -> 0+0.3*10
            assertEquals("27", a.quantile(0.9).toCanonicalString());    // p=2.7 -> 20+0.7*10
        }

        @Test
        void quarterQuantiles() {
            // [0,1,2,3,4], p=4q
            var a = acc(0, 1, 2, 3, 4);
            assertEquals("1", a.quantile(0.25).toCanonicalString());
            assertEquals("2", a.quantile(0.5).toCanonicalString());
            assertEquals("3", a.quantile(0.75).toCanonicalString());
        }
    }

    @Nested
    class ErrorsAndEmptiness {

        @Test
        void quantileOnEmptyAccumulatorThrows() {
            var a = new TreeMapAccumulator();
            assertThrows(IllegalStateException.class, () -> a.quantile(0.5));
        }

        @Test
        void qOutsideUnitIntervalThrows() {
            var a = acc(1, 2, 3);
            assertThrows(IllegalArgumentException.class, () -> a.quantile(-0.01));
            assertThrows(IllegalArgumentException.class, () -> a.quantile(1.01));
            assertThrows(IllegalArgumentException.class, () -> a.quantile(Double.NaN));
        }

        @Test
        void removingAbsentValueThrows() {
            var a = new TreeMapAccumulator();
            a.add(5);
            a.remove(5);
            assertThrows(IllegalStateException.class, () -> a.remove(5));
        }
    }
}
