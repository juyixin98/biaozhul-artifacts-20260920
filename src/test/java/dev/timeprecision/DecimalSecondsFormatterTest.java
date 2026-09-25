package dev.timeprecision;

import org.junit.jupiter.api.Test;

import java.math.BigInteger;
import java.math.RoundingMode;

import static org.junit.jupiter.api.Assertions.assertEquals;

class DecimalSecondsFormatterTest {

    @Test
    void formatsWholeSecondsWithoutFraction() {
        assertEquals("2", DecimalSecondsFormatter.format(BigInteger.valueOf(2L), TimeUnit.SECONDS));
        assertEquals("0", DecimalSecondsFormatter.format(BigInteger.ZERO, TimeUnit.NANOSECONDS));
    }

    @Test
    void stripsTrailingFractionZeros() {
        assertEquals("-1.5", DecimalSecondsFormatter.format(BigInteger.valueOf(-1500L), TimeUnit.MILLISECONDS));
        assertEquals("123.456789012", DecimalSecondsFormatter.format(new BigInteger("123456789012"), TimeUnit.NANOSECONDS));
    }

    @Test
    void formatsNegativeSubSecondValues() {
        assertEquals("-0.5", DecimalSecondsFormatter.format(BigInteger.valueOf(-500L), TimeUnit.MILLISECONDS));
        assertEquals("-0.000000001", DecimalSecondsFormatter.format(BigInteger.valueOf(-1L), TimeUnit.NANOSECONDS));
    }

    @Test
    void roundTripThroughTextIsLosslessForRepresentableValues() {
        BigInteger[] nanos = {
                BigInteger.ZERO,
                BigInteger.ONE,
                BigInteger.ONE.negate(),
                new BigInteger("123456789012"),
                new BigInteger("-987654321098765432"),
                BigInteger.valueOf(Long.MAX_VALUE),
                BigInteger.valueOf(Long.MIN_VALUE),
        };
        for (BigInteger v : nanos) {
            String text = DecimalSecondsFormatter.format(v, TimeUnit.NANOSECONDS);
            assertEquals(v, DecimalSecondsParser.toNanos(text), "round trip failed for " + v);
        }
    }

    @Test
    void roundTripAcrossUnitsIsLosslessWhenRepresentable() {
        // Millisecond values are exactly representable as nanoseconds and as text.
        long[] millis = {0L, 1L, -1L, 86_400_000L, -86_400_001L, Long.MAX_VALUE / 1_000_000L};
        for (long ms : millis) {
            String text = DecimalSecondsFormatter.format(BigInteger.valueOf(ms), TimeUnit.MILLISECONDS);
            long back = DecimalSecondsParser.parse(text, TimeUnit.MILLISECONDS, RoundingMode.UNNECESSARY);
            assertEquals(ms, back);
        }
    }
}
