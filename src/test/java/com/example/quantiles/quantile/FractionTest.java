package com.example.quantiles.quantile;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class FractionTest {

    @Test
    void integerValuesRenderWithoutDecimalPoint() {
        assertEquals("0", Fraction.of(0).toCanonicalString());
        assertEquals("7", Fraction.of(7).toCanonicalString());
        assertEquals("-13", Fraction.of(-13).toCanonicalString());
    }

    @Test
    void averagesAreExactHalvesIncludingNegatives() {
        assertEquals("2.5", Fraction.average(2, 3).toCanonicalString());
        assertEquals("-2.5", Fraction.average(-3, -2).toCanonicalString());
        assertEquals("-0.5", Fraction.average(-1, 0).toCanonicalString());
        assertEquals("0", Fraction.average(-2, 2).toCanonicalString());
    }

    @Test
    void averagesDoNotOverflowAtLongBoundaries() {
        Fraction f = Fraction.average(Long.MAX_VALUE, Long.MAX_VALUE - 1);
        assertEquals("9223372036854775806.5", f.toCanonicalString());
        Fraction g = Fraction.average(Long.MIN_VALUE, Long.MIN_VALUE + 1);
        // (MIN + MIN+1)/2 = MIN + 0.5
        assertEquals("-9223372036854775807.5", g.toCanonicalString());
        Fraction h = Fraction.average(Long.MIN_VALUE, Long.MAX_VALUE);
        assertEquals("-0.5", h.toCanonicalString());
    }

    @Test
    void fractionsAreReduced() {
        assertEquals(2L, new Fraction(java.math.BigInteger.valueOf(4), 8).denominator());
        assertEquals(1L, new Fraction(java.math.BigInteger.valueOf(4), 8).numerator().longValue());
        assertEquals("2", new Fraction(java.math.BigInteger.valueOf(4), 2).toCanonicalString());
        assertEquals("-1/2", new Fraction(java.math.BigInteger.valueOf(-3), 6).toString());
    }

    @Test
    void denominatorMustBePositive() {
        assertThrows(IllegalArgumentException.class,
                () -> new Fraction(java.math.BigInteger.ONE, 0));
        assertThrows(IllegalArgumentException.class,
                () -> new Fraction(java.math.BigInteger.ONE, -2));
    }
}
