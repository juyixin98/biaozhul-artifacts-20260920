package dev.timeprecision;

import org.junit.jupiter.api.Test;

import java.math.BigInteger;
import java.math.RoundingMode;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class TimeConverterTest {

    private static long convert(long value, TimeUnit from, TimeUnit to, RoundingMode mode) {
        return TimeConverter.convert(BigInteger.valueOf(value), from, to, mode);
    }

    @Test
    void upscalingIsAlwaysExact() {
        assertEquals(1_000_000_000L, convert(1L, TimeUnit.SECONDS, TimeUnit.NANOSECONDS, RoundingMode.UNNECESSARY));
        assertEquals(1_500_000_000L, convert(1500L, TimeUnit.MILLISECONDS, TimeUnit.NANOSECONDS, RoundingMode.UNNECESSARY));
        assertEquals(-2_500_000_000L, convert(-2_500_000L, TimeUnit.MICROSECONDS, TimeUnit.NANOSECONDS, RoundingMode.UNNECESSARY));
    }

    @Test
    void identityConversionKeepsExtremeValues() {
        assertEquals(Long.MAX_VALUE, convert(Long.MAX_VALUE, TimeUnit.NANOSECONDS, TimeUnit.NANOSECONDS, RoundingMode.UNNECESSARY));
        assertEquals(Long.MIN_VALUE, convert(Long.MIN_VALUE, TimeUnit.SECONDS, TimeUnit.SECONDS, RoundingMode.UNNECESSARY));
    }

    // ---- negative-value rounding (the floor/truncation divergence) ----

    @Test
    void floorRoundsTowardNegativeInfinity() {
        assertEquals(-2L, convert(-1500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.FLOOR));
        assertEquals(-1L, convert(-500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.FLOOR));
    }

    @Test
    void downTruncatesTowardZero() {
        assertEquals(-1L, convert(-1500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.DOWN));
        assertEquals(0L, convert(-500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.DOWN));
    }

    @Test
    void ceilingRoundsTowardPositiveInfinity() {
        assertEquals(-1L, convert(-1500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.CEILING));
        assertEquals(2L, convert(1500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.CEILING));
    }

    @Test
    void upRoundsAwayFromZero() {
        assertEquals(-2L, convert(-1500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.UP));
        assertEquals(1L, convert(500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.UP));
    }

    @Test
    void halfUpRoundsTiesAwayFromZero() {
        assertEquals(2L, convert(1500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.HALF_UP));
        assertEquals(-2L, convert(-1500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.HALF_UP));
        assertEquals(1L, convert(1499L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.HALF_UP));
    }

    @Test
    void halfDownRoundsTiesTowardZero() {
        assertEquals(1L, convert(1500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.HALF_DOWN));
        assertEquals(-1L, convert(-1500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.HALF_DOWN));
        assertEquals(2L, convert(1501L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.HALF_DOWN));
    }

    @Test
    void halfEvenRoundsTiesToEvenNeighbor() {
        assertEquals(2L, convert(1500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.HALF_EVEN));
        assertEquals(2L, convert(2500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.HALF_EVEN));
        assertEquals(-2L, convert(-1500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.HALF_EVEN));
        assertEquals(-2L, convert(-2500L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.HALF_EVEN));
    }

    @Test
    void unnecessaryAcceptsExactAndRejectsInexact() {
        assertEquals(2L, convert(2000L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.UNNECESSARY));
        ConversionException e = assertThrows(ConversionException.class,
                () -> convert(2001L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.UNNECESSARY));
        assertEquals(ErrorCode.ROUNDING_NECESSARY, e.code());
    }

    // ---- extreme integers and overflow ----

    @Test
    void maxLongSecondsToNanosOverflows() {
        ConversionException e = assertThrows(ConversionException.class,
                () -> convert(Long.MAX_VALUE, TimeUnit.SECONDS, TimeUnit.NANOSECONDS, RoundingMode.UNNECESSARY));
        assertEquals(ErrorCode.OVERFLOW, e.code());
    }

    @Test
    void minLongSecondsToMillisOverflows() {
        ConversionException e = assertThrows(ConversionException.class,
                () -> convert(Long.MIN_VALUE, TimeUnit.SECONDS, TimeUnit.MILLISECONDS, RoundingMode.UNNECESSARY));
        assertEquals(ErrorCode.OVERFLOW, e.code());
    }

    @Test
    void maxLongMillisToMicrosOverflows() {
        ConversionException e = assertThrows(ConversionException.class,
                () -> convert(Long.MAX_VALUE, TimeUnit.MILLISECONDS, TimeUnit.MICROSECONDS, RoundingMode.UNNECESSARY));
        assertEquals(ErrorCode.OVERFLOW, e.code());
    }

    @Test
    void largestRepresentableSecondsToNanosSucceeds() {
        long maxSecondsInNanos = Long.MAX_VALUE / 1_000_000_000L;
        assertEquals(maxSecondsInNanos * 1_000_000_000L,
                convert(maxSecondsInNanos, TimeUnit.SECONDS, TimeUnit.NANOSECONDS, RoundingMode.UNNECESSARY));
    }

    @Test
    void downscalingExtremeValuesIsExactWhenDivisible() {
        assertEquals(Long.MAX_VALUE / 1_000L,
                convert(Long.MAX_VALUE - (Long.MAX_VALUE % 1_000L), TimeUnit.NANOSECONDS, TimeUnit.MICROSECONDS, RoundingMode.UNNECESSARY));
    }

    // ---- round trips (lossless only where representable) ----

    @Test
    void roundTripThroughFinerUnitIsLossless() {
        long[] values = {0L, 1L, -1L, 1500L, -1500L, Long.MAX_VALUE / 1_000_000L, Long.MIN_VALUE / 1_000_000L};
        for (long v : values) {
            long nanos = convert(v, TimeUnit.MILLISECONDS, TimeUnit.NANOSECONDS, RoundingMode.UNNECESSARY);
            assertEquals(v, convert(nanos, TimeUnit.NANOSECONDS, TimeUnit.MILLISECONDS, RoundingMode.UNNECESSARY));
        }
    }

    @Test
    void roundTripThroughCoarserUnitIsLosslessOnlyWhenRepresentable() {
        // 2000 ms is representable in whole seconds: lossless.
        long seconds = convert(2000L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.UNNECESSARY);
        assertEquals(2000L, convert(seconds, TimeUnit.SECONDS, TimeUnit.MILLISECONDS, RoundingMode.UNNECESSARY));
        // 2001 ms is not: any downscaling must round, so no lossless round trip is promised.
        assertEquals(2000L, convert(2001L, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, RoundingMode.DOWN) * 1_000L);
    }

    // ---- input validation ----

    @Test
    void parseLongStrictRejectsMalformedInput() {
        for (String bad : new String[]{"", " ", "1.5", "1e3", "abc", "--1", "+", "0x10"}) {
            ConversionException e = assertThrows(ConversionException.class,
                    () -> TimeConverter.parseLongStrict(bad), "expected rejection of: " + bad);
            assertEquals(ErrorCode.INVALID_VALUE, e.code());
        }
    }

    @Test
    void parseLongStrictRejectsOutOfRangeLiterals() {
        ConversionException e = assertThrows(ConversionException.class,
                () -> TimeConverter.parseLongStrict("9223372036854775808"));
        assertEquals(ErrorCode.OVERFLOW, e.code());
        e = assertThrows(ConversionException.class,
                () -> TimeConverter.parseLongStrict("-9223372036854775809"));
        assertEquals(ErrorCode.OVERFLOW, e.code());
    }

    @Test
    void parseLongStrictAcceptsExtremes() {
        assertEquals(Long.MAX_VALUE, TimeConverter.parseLongStrict("9223372036854775807"));
        assertEquals(Long.MIN_VALUE, TimeConverter.parseLongStrict("-9223372036854775808"));
        assertEquals(0L, TimeConverter.parseLongStrict("-0"));
    }
}
